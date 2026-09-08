"""Bounded, read-only WHOOP v2 client with safe errors and resumable pages."""

import math
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .contracts import timestamp

API_BASE = "https://api.prod.whoop.com/developer/v2"
RESOURCE_PATHS = {
    "cycle": "/cycle",
    "recovery": "/recovery",
    "sleep": "/activity/sleep",
    "workout": "/activity/workout",
    "profile": "/user/profile/basic",
    "body": "/user/measurement/body",
}
COLLECTIONS = frozenset({"cycle", "recovery", "sleep", "workout"})
POLICY_HEADERS = frozenset(
    {
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "retry-after",
        "deprecation",
        "sunset",
        "cache-control",
        "expires",
        "date",
        "etag",
        "last-modified",
    }
)


class WhoopAPIError(ValueError):
    def __init__(self, message: str, status_code: int | None = None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


class WhoopClient:
    def __init__(
        self,
        oauth,
        transport=None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        max_pages: int = 200,
        max_records: int = 5000,
        max_retry_delay: float = 30,
    ):
        if not 0 <= max_retries <= 5 or not 1 <= max_pages <= 1000 or max_records < 1:
            raise ValueError("Invalid WHOOP request budget")
        if not 0 <= max_retry_delay <= 30:
            raise ValueError("Retry delay cap must be at most 30 seconds")
        self.oauth = oauth
        self.transport = transport
        self.sleep = sleep
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.max_records = max_records
        self.max_retry_delay = max_retry_delay

    @staticmethod
    def _headers(response):
        return {
            k.lower(): v[:512] for k, v in response.headers.items() if k.lower() in POLICY_HEADERS
        }

    def _retry_delay(self, response, attempt):
        raw = response.headers.get("retry-after")
        if raw is None and response.status_code == 429:
            raw = response.headers.get("x-ratelimit-reset")
        if raw is not None:
            try:
                seconds = float(raw)
                if not math.isfinite(seconds):
                    raise ValueError
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(raw)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    seconds = (parsed - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = 2**attempt
            if seconds > self.max_retry_delay:
                # Do not retry earlier than WHOOP permits, or block for an unbounded period.
                raise WhoopAPIError(
                    "WHOOP retry window exceeds this request's wait budget; resume later",
                    response.status_code,
                    self._headers(response),
                )
            return max(0, seconds)
        return min(self.max_retry_delay, 2**attempt)

    def _get(self, path, params):
        token = self.oauth.access_token()
        refreshed = False
        retries = 0
        with httpx.Client(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=20,
        ) as client:
            while True:
                try:
                    response = client.get(
                        API_BASE + path,
                        params=params,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )
                except httpx.HTTPError:
                    if retries >= self.max_retries:
                        raise WhoopAPIError("WHOOP request failed after bounded retries") from None
                    self.sleep(min(self.max_retry_delay, 2**retries))
                    retries += 1
                    continue
                if response.status_code == 401 and not refreshed:
                    token = self.oauth.access_token(force_refresh=True, rejected_token=token)
                    refreshed = True
                    continue
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if retries < self.max_retries:
                        self.sleep(self._retry_delay(response, retries))
                        retries += 1
                        continue
                if response.status_code != 200:
                    raise WhoopAPIError(
                        f"WHOOP request failed (HTTP {response.status_code})",
                        response.status_code,
                        self._headers(response),
                    )
                return response

    def list_records(self, resource, start=None, end=None, next_token=None) -> dict:
        """Fetch one page. A checkpoint may safely retain its exact next_token and window."""
        if resource not in RESOURCE_PATHS:
            raise WhoopAPIError("Unsupported WHOOP resource")
        params = {}
        if resource in COLLECTIONS:
            try:
                if not start or not end or timestamp(start) >= timestamp(end):
                    raise ValueError
            except (ValueError, TypeError):
                raise WhoopAPIError(
                    "Collection queries require a valid bounded time window"
                ) from None
            params = {"start": start, "end": end, "limit": 25}
            if next_token is not None:
                if not isinstance(next_token, str) or not next_token or len(next_token) > 8192:
                    raise WhoopAPIError("Invalid pagination checkpoint")
                params["nextToken"] = next_token
        elif next_token is not None:
            raise WhoopAPIError("This resource has no pagination")
        response = self._get(RESOURCE_PATHS[resource], params)
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            if resource in COLLECTIONS:
                records = payload["records"]
                following = payload.get("next_token")
                if following == "":
                    following = None
                if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
                    raise ValueError
                if len(records) > min(25, self.max_records):
                    raise ValueError
                if following is not None and (
                    not isinstance(following, str) or len(following) > 8192
                ):
                    raise ValueError
            else:
                records, following = [payload], None
        except (ValueError, KeyError, TypeError):
            raise WhoopAPIError("WHOOP returned an invalid resource envelope") from None
        return {"records": records, "next_token": following, "headers": self._headers(response)}

    def iter_pages(self, resource, start=None, end=None, next_token=None) -> Iterator[dict]:
        seen = {next_token} if next_token is not None else set()
        total = 0
        for _ in range(self.max_pages):
            page = self.list_records(resource, start, end, next_token)
            total += len(page["records"])
            if total > self.max_records:
                raise WhoopAPIError("WHOOP record budget exceeded")
            following = page["next_token"]
            if following is not None and following in seen:
                raise WhoopAPIError("WHOOP pagination repeated a checkpoint")
            yield page
            if following is None:
                return
            seen.add(following)
            next_token = following
        raise WhoopAPIError("WHOOP page budget exceeded; resume from the saved checkpoint")

    def cycle_by_id(self, cycle_id: int) -> dict:
        if type(cycle_id) is not int or cycle_id <= 0:
            raise WhoopAPIError("Invalid cycle identity")
        response = self._get(f"/cycle/{cycle_id}", {})
        try:
            record = response.json()
            if not isinstance(record, dict) or record.get("id") != cycle_id:
                raise ValueError
        except (ValueError, TypeError):
            raise WhoopAPIError("WHOOP returned an invalid linked cycle") from None
        return {"records": [record], "next_token": None, "headers": self._headers(response)}
