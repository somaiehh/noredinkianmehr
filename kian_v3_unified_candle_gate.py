
from kian_v3_candle_bridge import evaluate_candle_bridge

def prepare_unified_candles(engine, symbol, cutoff_ms):
    assessment = evaluate_candle_bridge(
        engine, symbol, cutoff_ms
    )

    # Fail closed. Never pass unverified candles
    # to process_unified_wave.
    return {
        "candle_data": None,
        "cutoff_ms": cutoff_ms,
        "candle_gate": {
            "status": "BLOCKED",
            "reason": assessment.get("reason", "UNVERIFIED"),
            "counts": assessment.get("candle_counts", {}),
            "confirmation_allowed": False,
        },
        "real_alert_sent": False,
    }
