
from kian_v3_multi_scan_ranker import (
    build_multi_scan_ranking,
)


EXCLUDED = {
    "BTCIRT", "XRPIRT", "ADAIRT", "SOLIRT",
    "USDTIRT",
}


def build_safe_pipeline(
    scan_results,
    allowed_irt_symbols,
):
    if not isinstance(scan_results, list):
        raise ValueError("INVALID_SCAN_RESULTS")

    if not isinstance(allowed_irt_symbols, (set, frozenset)):
        raise ValueError("INVALID_MARKET_UNIVERSE")

    allowed = set(allowed_irt_symbols) - EXCLUDED

    if not allowed:
        raise ValueError("EMPTY_ALLOWED_UNIVERSE")

    unique = {}
    rejected = 0

    for result in scan_results:
        if not isinstance(result, dict):
            rejected += 1
            continue

        symbol = result.get("symbol")
        decision = result.get("final_decision")
        context = result.get("early_pump_context")

        if (
            symbol not in allowed
            or not isinstance(decision, dict)
            or decision.get("status") != "BLOCKED"
            or decision.get("real_alert_sent") is not False
            or result.get("real_alert_sent") is not False
            or not isinstance(context, dict)
            or context.get("symbol") not in (None, symbol)
        ):
            rejected += 1
            continue

        if symbol in unique:
            rejected += 1
            continue

        normalized = dict(result)
        normalized_context = dict(context)

        if normalized_context.get("symbol") is None:
            normalized_context["symbol"] = symbol

        normalized["early_pump_context"] = normalized_context
        unique[symbol] = normalized

    ranking = build_multi_scan_ranking(
        list(unique.values()),
        allowed,
    )

    ranking["pipeline_version"] = "KIAN_V3_SAFE_V1"
    ranking["pipeline_rejected"] = rejected
    ranking["pipeline_received"] = len(scan_results)
    ranking["status"] = "MONITOR_ONLY"
    ranking["real_alert_sent"] = False

    return ranking
