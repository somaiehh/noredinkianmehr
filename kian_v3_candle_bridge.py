
from kian_v3_candle_adapter import build_candles

REQUIRED = {
    "15m": 9,
    "1h": 4,
    "4h": 4,
}

def evaluate_candle_bridge(engine, symbol, cutoff_ms):
    blocked = {
        "status": "BLOCKED",
        "confirmation_allowed": False,
        "real_alert_sent": False,
    }

    if type(cutoff_ms) is not int or cutoff_ms <= 0:
        return {
            **blocked,
            "reason": "INVALID_CUTOFF",
        }

    try:
        snapshot = build_candles(symbol, cutoff_ms)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {
            **blocked,
            "reason": "CANDLE_SOURCE_UNAVAILABLE",
            "error_type": type(e).__name__,
        }

    counts = {
        name: len(snapshot["candle_data"].get(name, []))
        for name in REQUIRED
    }

    # Coverage must be independently established.
    # Never trust a simple VERIFIED label as proof.
    if (
        snapshot.get("coverage") != "VERIFIED"
        or not snapshot.get("confirmation_allowed")
    ):
        return {
            **blocked,
            "reason": "TRADE_COVERAGE_UNVERIFIED",
            "candle_counts": counts,
        }

    missing = {
        name: REQUIRED[name] - counts[name]
        for name in REQUIRED
        if counts[name] < REQUIRED[name]
    }

    if missing:
        return {
            **blocked,
            "reason": "INSUFFICIENT_CLOSED_CANDLES",
            "candle_counts": counts,
            "missing": missing,
        }

    # Even a VERIFIED label cannot authorize trading.
    # The existing engine checks candle quality for diagnostics.
    try:
        diagnostic = engine.derive_candle_confirmation(
            snapshot["candle_data"],
            cutoff_ms,
        )
    except Exception as e:
        return {
            **blocked,
            "reason": "CANDLE_ENGINE_ERROR",
            "error_type": type(e).__name__,
        }

    return {
        **blocked,
        "reason": "INDEPENDENT_COVERAGE_PROOF_REQUIRED",
        "candle_counts": counts,
        "candle_diagnostic": diagnostic,
    }
