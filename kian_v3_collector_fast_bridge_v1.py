from math import isfinite


def build_fast_observation(symbol, anchor_time, scan_time, trades):
    """Convert collected trades into conservative FAST observations."""

    if not isinstance(symbol, str) or not symbol.endswith("IRT"):
        raise ValueError("INVALID_SYMBOL")

    if type(anchor_time) is not int or anchor_time <= 0:
        raise ValueError("INVALID_ANCHOR")

    if type(scan_time) is not int or scan_time < anchor_time:
        raise ValueError("INVALID_SCAN_TIME")

    if not isinstance(trades, list):
        raise ValueError("INVALID_TRADES")

    valid = []
    seen = set()
    rejected = 0

    for row in trades:
        if not isinstance(row, dict):
            rejected += 1
            continue

        try:
            tid = row["id"]
            tm = row["time"]
            price = float(row["price"])
            qty = float(row["qty"])
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected += 1
            continue

        if (
            type(tid) is not int
            or tid <= 0
            or type(tm) is not int
            or tm <= 0
            or tm > scan_time
            or not isfinite(price)
            or not isfinite(qty)
            or price <= 0
            or qty <= 0
        ):
            rejected += 1
            continue

        if tid in seen:
            rejected += 1
            continue

        seen.add(tid)
        valid.append((tm, tid, price, qty))

    valid.sort()

    windows = {}

    for minutes in (1, 3, 5):
        start = max(anchor_time, scan_time - minutes * 60000)

        rows = [
            row for row in valid
            if start <= row[0] <= scan_time
        ]

        windows[f"{minutes}m"] = {
            "trades": len(rows),
            "volume": round(
                sum(price * qty for _, _, price, qty in rows),
                4,
            ),
            "coverage_status": "UNVERIFIED",
        }

    latest = [
        row for row in valid
        if anchor_time <= row[0] <= scan_time
    ]

    last = latest[-1] if latest else None

    return {
        "symbol": symbol,
        "anchor_time": anchor_time,
        "time": last[0] if last else None,
        "price": last[2] if last else None,
        **windows,
        "valid_trade_count": len(valid),
        "rejected_trade_count": rejected,
        "coverage_verified": False,
        "evidence_status": "FAST_OBSERVED",
        "approved_for_trade": False,
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }


def validate_fast_observation_identity(
    symbol, anchor_time, scan_time, trades, observation
):
    """Check internal consistency; does not certify trade coverage."""
    if not isinstance(observation, dict):
        return False

    try:
        expected = build_fast_observation(
            symbol, anchor_time, scan_time, trades
        )
    except (ValueError, TypeError, KeyError):
        return False

    if observation != expected:
        return False

    return (
        expected["symbol"] == symbol
        and expected["anchor_time"] == anchor_time
        and expected["coverage_verified"] is False
        and expected["evidence_status"] == "FAST_OBSERVED"
        and expected["approved_for_trade"] is False
        and expected["real_alert_sent"] is False
    )
