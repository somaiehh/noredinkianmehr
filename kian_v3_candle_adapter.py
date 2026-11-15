import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "kian_v3_segment_store.json"

FRAMES = {
    "15m": 900000,
    "1h": 3600000,
    "4h": 14400000,
}


def build_candles(symbol, cutoff_ms):
    if (
        not isinstance(symbol, str)
        or not symbol.endswith("IRT")
        or type(cutoff_ms) is not int
        or cutoff_ms <= 0
    ):
        raise ValueError("INVALID_PARAMETERS")

    state = json.loads(STORE.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise ValueError("UNKNOWN_STORE_VERSION")

    sections = state["markets"][symbol]["segments"]
    if not sections:
        raise ValueError("NO_SEGMENTS")

    latest = sections[-1]
    trades = latest["trades"]

    if latest.get("coverage") != "VERIFIED":
        coverage = "UNVERIFIED"
    else:
        coverage = "VERIFIED"

    result = {}

    for name, interval in FRAMES.items():
        buckets = {}

        for trade in trades:
            ts = trade["time"]

            if ts >= cutoff_ms:
                continue

            price = float(trade["price"])
            qty = float(trade["qty"])

            if (
                not math.isfinite(price)
                or not math.isfinite(qty)
                or price <= 0 or qty <= 0
            ):
                raise ValueError("INVALID_TRADE_VALUES")

            start = (ts // interval) * interval
            if start + interval > cutoff_ms:
                continue

            if start not in buckets:
                buckets[start] = {
                    "time": start,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 0.0,
                }

            candle = buckets[start]
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += price * qty

        result[name] = [
            buckets[t] for t in sorted(buckets)
        ]

    return {
        "symbol": symbol,
        "coverage": coverage,
        "candle_data": result,
        "confirmation_allowed": coverage == "VERIFIED",
        "real_alert_sent": False,
    }


if __name__ == "__main__":
    import time
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCIRT"
    cutoff = int(time.time() * 1000)
    output = build_candles(symbol, cutoff)

    print("=== KIAN V3 CANDLE ADAPTER ===")
    print("SYMBOL:", output["symbol"])
    print("COVERAGE:", output["coverage"])

    for name, rows in output["candle_data"].items():
        print(name, "CLOSED_CANDLES:", len(rows))

    print("CONFIRMATION_ALLOWED:", output["confirmation_allowed"])
    print("NO_REAL_ALERTS")
    print("NO_PRODUCTION_CHANGES")
