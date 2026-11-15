
from kian_v3_feature_adapter_v1 import build_features


def build_safe_feature_bridge(
    symbol, anchor_time, cutoff, fast, radar
):
    features = build_features(
        symbol=symbol,
        anchor_time=anchor_time,
        cutoff=cutoff,
        fast=fast,
        radar=radar,
    )

    radar_rows = [
        r for r in radar.get(symbol, [])
        if isinstance(r, dict)
        and type(r.get("time")) is int
        and anchor_time <= r["time"] <= cutoff
        and r["time"] >= cutoff - 1800000
    ]

    radar_row = (
        max(radar_rows, key=lambda r: r["time"])
        if radar_rows else None
    )

    breakout = (
        radar_row.get("breakout")
        if radar_row else None
    )

    if type(breakout) is not bool:
        breakout = None

    features["radar"]["breakout"] = breakout

    import math

    def valid_metric(value):
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and value >= 0
        )

    ta = radar_row.get("ta") if radar_row else None
    book = radar_row.get("book") if radar_row else None

    if not valid_metric(ta):
        ta = None

    if not valid_metric(book) or book > 1:
        book = None

    features["radar"]["ta"] = ta
    features["radar"]["book"] = book

    features["radar"]["ta_status"] = (
        "AVAILABLE" if ta is not None else "UNKNOWN"
    )
    features["radar"]["book_status"] = (
        "AVAILABLE" if book is not None else "UNKNOWN"
    )

    features["status"] = "MONITOR_ONLY"
    features["real_alert_sent"] = False

    return features
