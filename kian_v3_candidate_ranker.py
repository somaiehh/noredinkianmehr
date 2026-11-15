
import math


def _number(value):
    if type(value) not in (int, float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def rank_candidates(rows, top_limit=10, watch_limit=10):
    if not isinstance(rows, list):
        raise ValueError("INVALID_ROWS")

    if (
        type(top_limit) is not int
        or type(watch_limit) is not int
        or not 1 <= top_limit <= 100
        or not 1 <= watch_limit <= 100
    ):
        raise ValueError("INVALID_LIMIT")

    candidates = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        symbol = row.get("symbol")
        if (
            not isinstance(symbol, str)
            or not symbol.isascii()
            or not symbol.isalnum()
            or symbol != symbol.upper()
            or not symbol.endswith("IRT")
            or len(symbol) <= 3
        ):
            continue

        context = row.get("early_pump_context")
        if not isinstance(context, dict):
            continue

        if context.get("symbol") != symbol:
            continue

        stage = context.get("stage")
        candidate_status = context.get("candidate_status")

        if candidate_status not in ("PRE_EARLY", "EARLY"):
            category = "WATCH"
        elif stage == "EARLY_CANDIDATE_UNVERIFIED":
            category = "TOP"
        else:
            category = "WATCH"

        score = _number(context.get("hunt_score"))
        ignition = context.get("ignition") or {}
        rank = context.get("rank") or {}

        ignition_seen = (
            isinstance(ignition, dict)
            and ignition.get("seen") is True
        )
        momentum = (
            isinstance(rank, dict)
            and rank.get("momentum") is True
        )

        report = row.get("candidate_report")
        observed_priority = (
            report.get("observed_activity_priority", "WAITING")
            if isinstance(report, dict) else "WAITING"
        )
        if observed_priority not in (
            "HIGH_WATCH", "NORMAL_WATCH", "WAITING", "INVALID"
        ):
            observed_priority = "WAITING"

        # Display ordering only; never changes hunt_score or approval.
        observed_order = {
            "HIGH_WATCH": 0,
            "NORMAL_WATCH": 1,
            "WAITING": 2,
            "INVALID": 3,
        }[observed_priority]

        priority = (
            (score if score is not None else 0)
            + (8 if ignition_seen else 0)
            + (5 if momentum else 0)
        )

        candidates.append({
            "symbol": symbol,
            "category": category,
            "stage": stage,
            "candidate_status": candidate_status,
            "hunt_score": score,
            "ignition_seen": ignition_seen,
            "momentum": momentum,
            "priority_score": round(priority, 3),
            "observed_activity_priority": observed_priority,
            "observed_order": observed_order,
            "risk_flags": context.get("risk_flags", []),
            "final_decision": "BLOCKED",
            "real_alert_sent": False,
        })

    # A symbol must not occupy multiple ranking slots.
    unique = {}
    for item in candidates:
        symbol = item["symbol"]
        previous = unique.get(symbol)
        if previous is None:
            unique[symbol] = item
        elif (
            item["category"] == "WATCH"
            or (
                previous["category"] == item["category"]
                and item["priority_score"] > previous["priority_score"]
            )
        ):
            unique[symbol] = item

    ordered = sorted(
        unique.values(),
        key=lambda x: (
            x["observed_order"],
            -x["priority_score"],
            x["symbol"],
        ),
    )

    top = [
        x for x in ordered if x["category"] == "TOP"
    ][:top_limit]

    watch = [
        x for x in ordered if x["category"] == "WATCH"
    ][:watch_limit]

    return {
        "top10": top,
        "watchlist": watch,
        "total_unique": len(unique),
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }
