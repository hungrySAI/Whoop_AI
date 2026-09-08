"""Daily reading aid projected from existing official views and registered statistics.

No data access, new scores, model calls or independent health calculations. The
caller supplies the same snapshot used for cards and the selected detailed trend.
"""


def _comparison(view):
    summary = view["summary"]
    enough = summary["status"] == "descriptive"
    change = summary["change"] if enough else None
    direction = (
        "unavailable"
        if change is None
        else "higher"
        if change > 0
        else "lower"
        if change < 0
        else "unchanged"
    )
    change_unit = "个百分点" if view["unit_label"] == "%" else "分"
    if change is None:
        statement = "样本不足，暂不比较变化。"
    elif change == 0:
        statement = "前后半段均值相同。"
    else:
        # Presentation only: the change itself comes exclusively from mean_change/1.
        magnitude = "不足 0.1" if abs(change) < 0.1 else f"{abs(change):.1f}"
        statement = f"后半段均值{'高' if change > 0 else '低'} {magnitude} {change_unit}。"
    return {
        "status": summary["status"],
        "change": change,
        "direction": direction,
        "statement": statement,
        "unit_label": change_unit,
        "first_half": summary["first_half"],
        "second_half": summary["second_half"],
        "midpoint": summary["midpoint"],
        "analysis_id": view["analysis_id"],
        "origin": view["summary_origin"],
    }


def build_daily_brief(cards, *, start, end, days):
    """Describe latest captured records without pretending they share a measurement day."""
    today = end[:10]
    items = []
    for view in cards:
        latest = view["latest"]
        recency = (
            "none"
            if latest is None
            else "today"
            if latest["measured_at"][:10] == today
            else "earlier"
        )
        interval_open = bool(
            view["key"] == "strain"
            and latest
            and (latest["measured_end"] is None or latest["measured_end"] > end)
        )
        items.append(
            {
                "key": view["key"],
                "latest_revision_id": latest["revision_id"] if latest else None,
                "recency": recency,
                "interval_open": interval_open,
                "context": "周期尚未结束，当前负荷可能继续更新。"
                if interval_open
                else "恢复随关联周期开始时间归属。"
                if view["key"] == "recovery" and latest
                else "最新睡眠记录，可能包含小睡。"
                if view["key"] == "sleep" and latest
                else "",
                "comparison": _comparison(view),
            }
        )
    today_count = sum(item["recency"] == "today" for item in items)
    any_records = any(item["recency"] != "none" for item in items)
    return {
        "as_of": end,
        "date": today,
        "start": start,
        "end": end,
        "days": days,
        "timezone": "UTC",
        "state": "today"
        if today_count == 3
        else "partial_today"
        if today_count
        else "historical"
        if any_records
        else "empty",
        "headline": f"今天已有 {today_count} 项记录，评分状态见下方。"
        if today_count
        else "今天尚无已采集记录，先查看最近状态。"
        if any_records
        else "当前窗口还没有已采集记录。",
        "items": items,
    }
