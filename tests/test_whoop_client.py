from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from whoop_copilot.whoop_client import WhoopAPIError, WhoopClient

START, END = "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"


class FakeOAuth:
    def __init__(self):
        self.refreshes = 0

    def access_token(self, force_refresh=False, rejected_token=None):
        if force_refresh:
            assert rejected_token == "synthetic-token"
            self.refreshes += 1
        return "synthetic-new-token" if self.refreshes else "synthetic-token"


def build(handler, **kwargs):
    return WhoopClient(FakeOAuth(), transport=httpx.MockTransport(handler), **kwargs)


def test_pages_retain_window_and_opaque_cursor_and_whitelist_headers():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.prod.whoop.com"
        assert request.url.params["start"] == START
        assert request.url.params["end"] == END
        assert request.url.params["limit"] == "25"
        if len(requests) == 1:
            return httpx.Response(200, json={"records": [{"id": 1}], "next_token": "opaque+/="})
        assert request.url.params["nextToken"] == "opaque+/="
        return httpx.Response(
            200,
            json={"records": [], "next_token": ""},
            headers={"X-RateLimit-Remaining": "99", "Sunset": "tomorrow", "Set-Cookie": "secret"},
        )

    pages = list(build(handler).iter_pages("sleep", START, END))
    assert len(pages) == 2
    assert pages[-1]["headers"] == {"x-ratelimit-remaining": "99", "sunset": "tomorrow"}
    assert pages[-1]["next_token"] is None


@pytest.mark.parametrize(
    "resource,path",
    [
        ("cycle", "/cycle"),
        ("recovery", "/recovery"),
        ("sleep", "/activity/sleep"),
        ("workout", "/activity/workout"),
        ("profile", "/user/profile/basic"),
        ("body", "/user/measurement/body"),
    ],
)
def test_resource_paths_and_singletons(resource, path):
    def handler(request):
        assert request.url.path == "/developer/v2" + path
        payload = {"records": []} if resource not in {"profile", "body"} else {"id": 1}
        if resource in {"profile", "body"}:
            assert not request.url.query
        return httpx.Response(200, json=payload)

    assert len(list(build(handler).iter_pages(resource, START, END))) == 1


def test_unknown_resource_rejected_before_oauth_and_network():
    client = build(lambda r: pytest.fail("No HTTP"))
    with pytest.raises(WhoopAPIError, match="Unsupported"):
        client.list_records("https://evil.example/steal", START, END)


@pytest.mark.parametrize("start,end", [(None, None), (END, START), ("bad", END)])
def test_collections_need_bounded_window(start, end):
    with pytest.raises(WhoopAPIError, match="bounded"):
        build(lambda r: pytest.fail("No HTTP")).list_records("cycle", start, end)


def test_cursor_loop_and_page_and_record_budgets():
    client = build(lambda r: httpx.Response(200, json={"records": [], "next_token": "same"}))
    with pytest.raises(WhoopAPIError, match="repeated"):
        list(client.iter_pages("sleep", START, END))
    client = build(
        lambda r: httpx.Response(200, json={"records": [], "next_token": "next"}),
        max_pages=1,
    )
    with pytest.raises(WhoopAPIError, match="page budget"):
        list(client.iter_pages("sleep", START, END))
    count = [0]

    def handler(request):
        count[0] += 1
        return httpx.Response(
            200, json={"records": [{"id": count[0]}], "next_token": str(count[0])}
        )

    with pytest.raises(WhoopAPIError, match="record budget"):
        list(build(handler, max_records=1).iter_pages("sleep", START, END))


def test_401_forces_refresh_exactly_once():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="sensitive user information")

    client = build(handler)
    with pytest.raises(WhoopAPIError, match="HTTP 401") as error:
        client.list_records("cycle", START, END)
    assert "sensitive" not in str(error.value)
    assert len(calls) == 2 and client.oauth.refreshes == 1
    assert calls[-1].headers["Authorization"] == "Bearer synthetic-new-token"


@pytest.mark.parametrize("status", [400, 403, 404, 302])
def test_nonretryable_errors_and_redirects_do_not_retry_or_leak(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="sensitive", headers={"Location": "https://evil.example"}
        )

    with pytest.raises(WhoopAPIError) as error:
        build(handler).list_records("cycle", START, END)
    assert "sensitive" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_bounded_retry_respects_seconds_and_date_retry_after(status):
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": "2"})
        if len(calls) == 2:
            date = format_datetime(datetime.now(UTC) + timedelta(seconds=4), usegmt=True)
            return httpx.Response(status, headers={"Retry-After": date})
        return httpx.Response(200, json={"records": []})

    client = build(handler, sleep=delays.append)
    client.list_records("cycle", START, END)
    assert delays[0] == 2
    assert 2 <= delays[1] <= 4
    assert len(calls) == 3


def test_long_retry_after_returns_without_retrying_early():
    delays = []
    client = build(
        lambda r: httpx.Response(429, headers={"Retry-After": "600"}),
        sleep=delays.append,
    )
    with pytest.raises(WhoopAPIError, match="wait budget") as error:
        client.list_records("cycle", START, END)
    assert delays == []
    assert error.value.headers == {"retry-after": "600"}


def test_transport_error_retries_are_bounded_and_sanitized():
    count, delays = [0], []

    def handler(request):
        count[0] += 1
        raise httpx.ReadTimeout("synthetic-token at sensitive URL", request=request)

    with pytest.raises(WhoopAPIError) as error:
        build(handler, sleep=delays.append, max_retries=2).list_records("cycle", START, END)
    assert count[0] == 3 and delays == [1, 2]
    assert "synthetic-token" not in str(error.value)


@pytest.mark.parametrize("payload", [[], {}, {"records": "wrong"}, {"records": [1]}])
def test_malformed_response_fails_without_dumping_payload(payload):
    with pytest.raises(WhoopAPIError, match="invalid resource envelope"):
        build(lambda r: httpx.Response(200, json=payload)).list_records("cycle", START, END)


@pytest.mark.parametrize("token", [False, 0, [], {}])
def test_falsy_invalid_cursor_does_not_silently_truncate(token):
    payload = {"records": [], "next_token": token}
    with pytest.raises(WhoopAPIError, match="invalid resource envelope"):
        build(lambda r: httpx.Response(200, json=payload)).list_records("cycle", START, END)


def test_api_http_configuration_disables_environment_and_redirects(monkeypatch):
    import whoop_copilot.whoop_client as module

    original, settings = module.httpx.Client, []

    def factory(**kwargs):
        settings.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module.httpx, "Client", factory)
    build(lambda r: httpx.Response(200, json={"records": []})).list_records("cycle", START, END)
    assert settings[0]["trust_env"] is False
    assert settings[0]["follow_redirects"] is False
    assert settings[0].get("verify", True) is True
