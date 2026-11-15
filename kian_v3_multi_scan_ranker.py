
from kian_v3_candidate_ranker import rank_candidates


def build_multi_scan_ranking(
    scan_results,
    allowed_irt_symbols,
    top_limit=10,
    watch_limit=10,
):
    if not isinstance(scan_results, list):
        raise ValueError("INVALID_SCAN_RESULTS")

    if not isinstance(allowed_irt_symbols, (set, frozenset)):
        raise ValueError("INVALID_MARKET_UNIVERSE")

    if not allowed_irt_symbols:
        raise ValueError("EMPTY_MARKET_UNIVERSE")

    valid_rows = []
    rejected = 0

    for result in scan_results:
        if not isinstance(result, dict):
            rejected += 1
            continue

        symbol = result.get("symbol")

        if symbol not in allowed_irt_symbols:
            rejected += 1
            continue

        decision = result.get("final_decision")
        context = result.get("early_pump_context")

        if (
            not isinstance(decision, dict)
            or decision.get("status") != "BLOCKED"
            or decision.get("real_alert_sent") is not False
            or result.get("real_alert_sent") is not False
            or not isinstance(context, dict)
            or context.get("symbol") != symbol
        ):
            rejected += 1
            continue

        valid_rows.append(result)

    ranking = rank_candidates(
        valid_rows,
        top_limit=top_limit,
        watch_limit=watch_limit,
    )

    ranking["scan_results_received"] = len(scan_results)
    ranking["scan_results_accepted"] = len(valid_rows)
    ranking["scan_results_rejected"] = rejected
    ranking["market_universe_checked"] = True
    ranking["status"] = "MONITOR_ONLY"
    ranking["real_alert_sent"] = False

    return ranking
