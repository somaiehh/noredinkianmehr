
from math import isfinite

def validate_fast_trades(
    symbol, anchor_time, scan_time, trades,
    requested_limit=1000
):
    if not isinstance(symbol, str) or not symbol.endswith("IRT"):
        raise ValueError("INVALID_SYMBOL")
    if type(anchor_time) is not int or anchor_time <= 0:
        raise ValueError("INVALID_ANCHOR")
    if type(scan_time) is not int or scan_time < anchor_time:
        raise ValueError("INVALID_SCAN_TIME")
    if not isinstance(trades, list):
        raise ValueError("INVALID_TRADES")
    if type(requested_limit) is not int or requested_limit <= 0:
        raise ValueError("INVALID_LIMIT")

    valid = []
    seen = set()
    invalid_count = 0
    duplicate_count = 0

    for t in trades:
        if not isinstance(t, dict):
            invalid_count += 1
            continue

        try:
            raw_time = t["time"]
            if type(raw_time) is not int:
                raise ValueError("INVALID_TIME")

            tm = raw_time
            price = float(t["price"])
            qty = float(t["qty"])
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid_count += 1
            continue

        if not (
            0 < tm <= scan_time
            and isfinite(price) and price > 0
            and isfinite(qty) and qty > 0
        ):
            invalid_count += 1
            continue

        trade_id = t.get("id")

        if trade_id is not None:
            key = str(trade_id)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)

        # V3_FAST_DIRECTION_METRICS_V1
        maker = t.get("isBuyerMaker")
        if type(maker) is not bool:
            maker = None
        quote = price * qty
        if not isfinite(quote) or quote <= 0:
            invalid_count += 1
            continue
        valid.append((tm, price, qty, maker, quote))

    valid.sort(key=lambda x: x[0])

    oldest = valid[0][0] if valid else None

    windows = {}
    for minutes in (1, 3, 5):
        start = max(anchor_time, scan_time - minutes * 60000)
        rows = [x for x in valid if start <= x[0] <= scan_time]

        if not valid or oldest is None:
            coverage = "INSUFFICIENT"
        elif invalid_count or duplicate_count:
            coverage = "UNVERIFIED"
        elif oldest > start:
            coverage = "INSUFFICIENT"
        else:
            coverage = "POSSIBLE"

        # V3_FAST_FRESHNESS_REPORT_V1
        if invalid_count or duplicate_count:
            freshness = "UNVERIFIED"
        elif not rows:
            freshness = "NO_RECENT_TRADES"
        else:
            freshness = "FRESH_UNVERIFIED"

        buy = sum(x[4] for x in rows if x[3] is False)
        sell = sum(x[4] for x in rows if x[3] is True)
        unknown = sum(x[4] for x in rows if x[3] is None)
        total = buy + sell + unknown
        known = buy + sell

        if not isfinite(total):
            raise ValueError("FAST_VOLUME_OVERFLOW")

        complete = bool(rows) and all(x[3] is not None for x in rows)
        pressure = (buy - sell) / known if complete and known > 0 else None
        move = (rows[-1][1] / rows[0][1] - 1) * 100 if len(rows) >= 2 else None

        if move is not None and not isfinite(move):
            move = None

        windows[f"{minutes}m"] = {
            "buy_volume": round(buy, 4),
            "sell_volume": round(sell, 4),
            "unknown_volume": round(unknown, 4),
            "buy_pressure": pressure,
            "direction_complete": complete,
            "direction_coverage_pct": round(100 * (known / total), 4) if total > 0 else None,
            "price_move_pct": move,
            "trades": len(rows),
            "volume": round(total, 4),
            "coverage_status": coverage,
            "freshness_status": freshness,
        }

    latest = [
        x for x in valid
        if anchor_time <= x[0] <= scan_time
    ]
    last = latest[-1] if latest else None

    return {
        "symbol": symbol,
        "anchor_time": anchor_time,
        "time": last[0] if last else None,
        "price": last[1] if last else None,
        **windows,
        "raw_trade_count": len(trades),
        "valid_trade_count": len(valid),
        "invalid_trade_count": invalid_count,
        "duplicate_trade_count": duplicate_count,
        "oldest_valid_trade_time": oldest,
        "limit_reached": len(trades) >= requested_limit,
        "coverage_verified": False,
        "evidence_status": "FAST_SOURCE_PENDING",
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }
