from math import isfinite

FAST_FIELDS = ("trades", "volume", "buy_pressure", "price_move")
RADAR_FIELDS = (
    "p15", "vr", "va", "bs", "breakout",
    "hunt_score", "sequence_score_v1",
    "trades15", "trades1h", "trades4h",
    "compression15", "structure1h", "structure4h"
)

def numeric(value):
    if type(value) in (int, float) and isfinite(value):
        return value
    return None

def valid_time(value):
    return type(value) is int and 0 < value < 10**14

def latest(rows, cutoff, start, max_age):
    candidates = [
        r for r in rows
        if isinstance(r, dict)
        and valid_time(r.get("time"))
        and max(start, cutoff - max_age) <= r["time"] <= cutoff
    ]
    return max(candidates, key=lambda r: r["time"]) if candidates else None

def build_features(symbol, anchor_time, cutoff, fast, radar,
                   max_age_ms=1800000):
    if (
        not isinstance(symbol, str)
        or not symbol.endswith("IRT")
        or not valid_time(anchor_time)
        or not valid_time(cutoff)
        or cutoff < anchor_time
        or type(max_age_ms) is not int
        or max_age_ms < 0
    ):
        raise ValueError("INVALID_REQUEST")

    fast_rows = [
        r for r in fast
        if isinstance(r, dict)
        and r.get("symbol") == symbol
        and r.get("anchor_time") == anchor_time
    ]
    fast_row = latest(fast_rows, cutoff, anchor_time, max_age_ms)
    radar_row = latest(
        radar.get(symbol, []), cutoff, anchor_time, max_age_ms
    )

    result = {
        "wave_id": f"{symbol}:{anchor_time}",
        "cutoff": cutoff,
        "fast_time": fast_row["time"] if fast_row else None,
        "radar_time": radar_row["time"] if radar_row else None,
        "fast": {},
        "radar": {}
    }

    for window in ("1m", "3m", "5m"):
        values = fast_row.get(window) if fast_row else None
        if not isinstance(values, dict):
            values = {}
        result["fast"][window] = {
            field: numeric(values.get(field))
            for field in FAST_FIELDS
        }

    result["radar"] = {
        field: numeric(radar_row.get(field)) if radar_row else None
        for field in RADAR_FIELDS
    }
    return result
