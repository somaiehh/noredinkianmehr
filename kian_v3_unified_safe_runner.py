
from kian_v3_unified_candle_gate import prepare_unified_candles

def run_unified_safe(
    engine,
    symbol,
    anchor_time,
    row,
    score_row=None,
    history=None,
    current=None,
    wake_data=None,
    now_ms=None,
    cutoff_ms=None,
    rank_result=None,
    scan_id=None,
):
    if cutoff_ms is None:
        raise ValueError("CUTOFF_REQUIRED")

    gate = prepare_unified_candles(
        engine, symbol, cutoff_ms
    )

    # Never forward unverified candles.
    result = engine.process_unified_wave(
        symbol=symbol,
        anchor_time=anchor_time,
        row=row,
        score_row=score_row,
        history=history,
        current=current,
        wake_data=wake_data,
        now_ms=now_ms,
        candle_data=None,
        cutoff_ms=cutoff_ms,
        fetch_depth=False,
    )

    decision = result.get("final_decision")
    if not isinstance(decision, dict):
        raise RuntimeError("INVALID_FINAL_DECISION")

    if decision.get("status") != "BLOCKED":
        raise RuntimeError("UNSAFE_FINAL_DECISION")

    result["candle_gate"] = gate["candle_gate"]

    # Diagnostic only: never authorize trading.
    result["candle_diagnostics"] = {
        "status": gate["candle_gate"]["status"],
        "reason": gate["candle_gate"]["reason"],
        "counts": gate["candle_gate"].get("counts", {}),
        "confirmation_allowed": False,
        "real_alert_sent": False,
    }

    fast_observation = (
        result.get("observation", {}).get("fast", {})
    )

    fast_context = {
        "symbol": symbol,
        "wave_id": f"{symbol}:{anchor_time}",
        "fast_state": fast_observation.get("state"),
        "ignition_seen": fast_observation.get("ignition_seen"),
    }

    result["hunt_context"] = engine.evaluate_hunt_context(
        score_row,
        fast_result=fast_context,
        rank_result=rank_result,
        anchor_time=anchor_time,
        scan_id=scan_id,
    )

    result["early_pump_context"] = (
        engine.evaluate_early_pump_context(
            result["hunt_context"],
            result.get("market_safety"),
        )
    )

    if (
        result["early_pump_context"]["final_decision"]["status"]
        != "BLOCKED"
    ):
        raise RuntimeError("UNSAFE_EARLY_PUMP_DECISION")


    result["ignition_alert_preview"] = (
        engine.build_ignition_alert_preview(
            result["early_pump_context"]
        )
    )
    if (
        result["ignition_alert_preview"]["real_alert_sent"] is not False
        or result["ignition_alert_preview"]["preview_only"] is not True
    ):
        raise RuntimeError("UNSAFE_IGNITION_PREVIEW")

    result["real_alert_sent"] = False
    return result
