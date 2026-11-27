# V3_DIRECTION_AWARE_RANKING_V1
# V3_FINAL_OBSERVATION_REFINEMENT_V1
"""Independent observed FAST ignition. Monitor only."""

from math import isfinite
from kian_v3_fast_validator_v1 import validate_fast_trades

WINDOWS = (1, 3, 5)

def positive(v):
    return (
        type(v) in (int, float)
        and isfinite(v)
        and v > 0
    )

def evaluate_observed_ignition(
    symbol, scan_time, trades, source_evidence
):
    result = {
        "symbol": symbol,
        "time": scan_time,
        "status": "MONITOR_ONLY",
        "classification": "INSUFFICIENT_EVIDENCE",
        "coverage_verified": False,
        "approved_for_trade": False,
        "real_alert_sent": False,
        "windows": {},
    }

    if (
        not isinstance(symbol, str)
        or not symbol.endswith("IRT")
        or type(scan_time) is not int
        or scan_time <= 600000
        or not isinstance(trades, list)
        or not isinstance(source_evidence, dict)
    ):
        return result

    if (
        source_evidence.get("symbol") != symbol
        or source_evidence.get("scan_time") != scan_time
        or source_evidence.get("coverage_verified")
        is not False
        or source_evidence.get("evidence_status")
        != "FAST_SOURCE_PENDING"
        or source_evidence.get("filtered_trade_count") != 0
        or source_evidence.get("rejected_count") != 0
    ):
        return result

    if not trades:
        return result

    if any(
        not isinstance(t, dict)
        or type(t.get("time")) is not int
        or not 0 < t["time"] <= scan_time
        or t.get("id") is None
        for t in trades
    ):
        return result

    if len({str(t["id"]) for t in trades}) != len(trades):
        return result

    anchor = scan_time - 600000

    current = validate_fast_trades(
        symbol, anchor, scan_time, trades
    )

    if current["invalid_trade_count"]:
        return result

    if current["duplicate_trade_count"]:
        return result

    oldest = current["oldest_valid_trade_time"]

    hits = []

    for minutes in WINDOWS:
        width = minutes * 60000
        boundary = scan_time - width
        start = boundary - width

        key = f"{minutes}m"

        result["windows"][key] = {
            "status": "INSUFFICIENT_HISTORY"
        }

        if oldest is None or oldest > start:
            continue

        previous_rows = [
            t for t in trades
            if start <= t["time"] < boundary
        ]

        current_rows = [
            t for t in trades
            if boundary <= t["time"] <= scan_time
        ]

        old = validate_fast_trades(
            symbol, start, boundary - 1,
            previous_rows
        )[key]

        new = validate_fast_trades(
            symbol, boundary, scan_time,
            current_rows
        )[key]

        pt = old["trades"]
        pv = old["volume"]

        ct = new["trades"]
        cv = new["volume"]

        # Conservative initial comparison.
        # Missing/zero baseline is not acceleration.

        if pt < 2 or not positive(pv):

            # V3_QUIET_ACTIVITY_BRIDGE_V1

            if minutes != 5 or pt >= 2:
                continue

            if ct < 3:
                continue

            history_start = boundary - 3600000

            if oldest > history_start:
                continue

            history_rows = [
                t for t in trades
                if history_start <= t["time"] < boundary
            ]

            if len(history_rows) < 3:
                continue

            history_volume = sum(
                float(t["price"]) * float(t["qty"])
                for t in history_rows
            )

            baseline_count = len(history_rows) / 12
            baseline_volume = history_volume / 12

            if not positive(baseline_volume):
                continue

            trade_ratio = ct / baseline_count
            volume_ratio = cv / baseline_volume

            move = new["price_move_pct"]
            pressure = new["buy_pressure"]

            if not all(
                isfinite(x)
                for x in (trade_ratio, volume_ratio)
            ):
                continue

            if not (
                trade_ratio >= 1.5
                and volume_ratio >= 3.0
                and positive(move)
                and new["direction_complete"] is True
                and positive(pressure)
                and 0.4 <= pressure <= 1.0
            ):
                continue

            result["windows"][key] = {
                "status": "ACTIVITY_SURGE_OBSERVED",
                "activity_path": "QUIET_ACTIVITY",
                "baseline_kind": "60M_OBSERVED_AVERAGE",
                "baseline_verified": False,
                "previous_trades": pt,
                "current_trades": ct,
                "previous_volume": pv,
                "current_volume": cv,
                "history_trades": len(history_rows),
                "history_volume": history_volume,
                "baseline_trades": baseline_count,
                "baseline_volume": baseline_volume,
                "trade_ratio": trade_ratio,
                "volume_ratio": volume_ratio,
                "price_move_pct": move,
                "buy_pressure": pressure,
                "coverage_verified": False,
                "approved_for_trade": False,
            }

            hits.append(
                (1, "ACTIVITY_SURGE_OBSERVED")
            )

            continue

        trade_ratio = ct / pt
        volume_ratio = cv / pv

        if not all(
            isfinite(x)
            for x in (trade_ratio, volume_ratio)
        ):
            continue

        move = new["price_move_pct"]
        pressure = new["buy_pressure"]

        accelerating = (
            trade_ratio >= 1.5
            and volume_ratio >= 1.5
        )

        ignition = accelerating and positive(move)

        buy_support = (
            ignition
            and new["direction_complete"] is True
            and positive(pressure)
        )

        classification = "NO_ACCELERATION"

        if accelerating:
            classification = "ACTIVITY_SURGE_OBSERVED"

        if ignition:
            classification = "IGNITION_OBSERVED"

        if buy_support:
            classification = "BUY_SUPPORTED_IGNITION"

        result["windows"][key] = {
            "status": classification,
            "previous_trades": pt,
            "current_trades": ct,
            "previous_volume": pv,
            "current_volume": cv,
            "trade_ratio": trade_ratio,
            "volume_ratio": volume_ratio,
            "price_move_pct": move,
            "buy_pressure": pressure,
            "coverage_verified": False,
        }

        priority = {
            "NO_ACCELERATION": 0,
            "ACTIVITY_SURGE_OBSERVED": 1,
            "IGNITION_OBSERVED": 2,
            "BUY_SUPPORTED_IGNITION": 3,
        }

        hits.append((
            priority[classification],
            classification
        ))

    if hits:
        result["classification"] = max(hits)[1]

    return result


# V3_OBSERVED_IGNITION_RANKING_V1

IGNITION_PRIORITY = {
    "BUY_SUPPORTED_IGNITION": 3,
    "IGNITION_OBSERVED": 2,
    "ACTIVITY_SURGE_OBSERVED": 1,
}

def rank_observed_ignition(
    source_evidence,
    scan_time,
    allowed_symbols,
):
    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")

    if not isinstance(source_evidence, dict):
        raise ValueError("INVALID_SOURCE_EVIDENCE")

    import math

    def finite_number(value):
        return (
            type(value) in (int, float)
            and math.isfinite(value)
        )

    rows = []

    for symbol, evidence in source_evidence.items():
        if (
            not isinstance(symbol, str)
            or not symbol.endswith("IRT")
            or symbol not in allowed_symbols
            or not isinstance(evidence, dict)
            or evidence.get("symbol") != symbol
            or evidence.get("scan_time") != scan_time
            or evidence.get("coverage_verified")
            is not False
            or evidence.get("evidence_status")
            != "FAST_SOURCE_PENDING"
        ):
            continue

        obs = evidence.get("ignition_observation")

        if not isinstance(obs, dict):
            continue

        if (
            obs.get("symbol") != symbol
            or obs.get("time") != scan_time
            or obs.get("status") != "MONITOR_ONLY"
            or obs.get("coverage_verified") is not False
            or obs.get("approved_for_trade") is not False
            or obs.get("real_alert_sent") is not False
        ):
            continue

        classification = obs.get("classification")

        if classification not in IGNITION_PRIORITY:
            continue

        windows = obs.get("windows", {})

        if not isinstance(windows, dict):
            continue

        supporting = []

        for key in ("1m", "3m", "5m"):
            w = windows.get(key)

            if not isinstance(w, dict):
                continue

            if w.get("coverage_verified") is not False:
                continue

            if w.get("status") not in IGNITION_PRIORITY:
                continue

            tr = w.get("trade_ratio")
            vr = w.get("volume_ratio")

            if not positive(tr) or not positive(vr):
                continue

            if tr < 1.5 or vr < 1.5:
                continue

            pm = w.get("price_move_pct")
            bp = w.get("buy_pressure")

            level = "ACTIVITY_SURGE_OBSERVED"

            if positive(pm):
                level = "IGNITION_OBSERVED"

                if positive(bp) and bp <= 1:
                    level = "BUY_SUPPORTED_IGNITION"

            # V3_QUIET_DIRECTION_ALIGNMENT_V1
            quiet = w.get("activity_path") == "QUIET_ACTIVITY"

            if quiet:
                if not (
                    key == "5m"
                    and w.get("baseline_kind") == "60M_OBSERVED_AVERAGE"
                    and w.get("baseline_verified") is False
                    and w.get("approved_for_trade") is False
                    and w.get("status") == "ACTIVITY_SURGE_OBSERVED"
                    and vr >= 3.0
                    and positive(pm)
                    and finite_number(bp)
                    and 0.4 <= bp <= 1.0
                ):
                    continue

                level = "ACTIVITY_SURGE_OBSERVED"

            elif w.get("status") != level:
                continue

            supporting.append({
                "window": key,
                "classification": level,
                "activity_path": (
                    "QUIET_ACTIVITY" if quiet
                    else "STANDARD_ACCELERATION"
                ),
                "strength": min(tr, vr),
                "trade_ratio": tr,
                "volume_ratio": vr,
                "price_move_pct": (
                    pm if finite_number(pm) else None
                ),
                "buy_pressure": (
                    bp if finite_number(bp) and -1 <= bp <= 1
                    else None
                ),
            })

        if not supporting:
            continue

        best_level = max(
            IGNITION_PRIORITY[x["classification"]]
            for x in supporting
        )

        if best_level != IGNITION_PRIORITY[classification]:
            continue

        strength = max(
            x["strength"] for x in supporting
            if IGNITION_PRIORITY[x["classification"]]
            == best_level
        )

        def direction(w):
            bp = w.get("buy_pressure")
            if bp is None or bp == 0:
                return 1
            return 2 if bp > 0 else 0

        best_window = max(
            supporting,
            key=lambda w: (
                direction(w),
                IGNITION_PRIORITY[w["classification"]],
                w["strength"],
            ),
        )

        direction_priority = direction(best_window)
        selected_classification = best_window["classification"]
        selected_priority = IGNITION_PRIORITY[selected_classification]
        selected_strength = best_window["strength"]

        rows.append({
            "symbol": symbol,
            "direction_priority": direction_priority,
            "direction_status": {
                2: "BUY_PRESSURE_POSITIVE",
                1: "NEUTRAL_OR_UNKNOWN",
                0: "SELL_PRESSURE_NEGATIVE",
            }[direction_priority],
            "ranking_window": best_window["window"],
            "activity_path": best_window["activity_path"],
            "scan_time": scan_time,
            "classification": selected_classification,
            "maximum_observed_classification": classification,
            "priority": selected_priority,
            "supporting_windows": len(supporting),
            "strength": selected_strength,
            "ranking_policy": "DIRECTION_ALIGNED_V2",
            "windows": supporting,
            "status": "MONITOR_ONLY",
            "coverage_verified": False,
            "approved_for_trade": False,
            "real_alert_sent": False,
        })

    rows.sort(key=lambda x: (
        -x["direction_priority"],
        -x["priority"],
        -x["supporting_windows"],
        -x["strength"],
        x["symbol"],
    ))

    for rank, row in enumerate(rows, 1):
        row["observed_rank"] = rank

    return {
        "scan_time": scan_time,
        "ranking_policy": "DIRECTION_ALIGNED_V2",
        "ranked_count": len(rows),
        "ranked_rows": rows,
        "top10": rows[:10],
        "watchlist": rows[10:20],
        "status": "MONITOR_ONLY",
        "approved_for_trade": False,
        "real_alert_sent": False,
    }


# V3_IGNITION_RANK_MEMORY_V1

def update_observed_rank_memory(
    history,
    ranking,
    scan_time,
    scanned_symbols,
    allowed_symbols,
    real_scan=False,
):
    if real_scan is not True:
        raise ValueError("REAL_SCAN_REQUIRED")

    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")

    if not isinstance(history, list) or len(history) > 5:
        raise ValueError("INVALID_RANK_HISTORY")

    if (
        not isinstance(ranking, dict)
        or ranking.get("scan_time") != scan_time
        or ranking.get("status") != "MONITOR_ONLY"
        or ranking.get("approved_for_trade") is not False
        or ranking.get("real_alert_sent") is not False
    ):
        raise ValueError("UNSAFE_RANKING")

    scanned = sorted({
        s for s in scanned_symbols
        if isinstance(s, str)
        and s.endswith("IRT")
        and s in allowed_symbols
    })

    # V3_RANK_POLICY_MEMORY_GUARD_V1
    policy = ranking.get("ranking_policy")

    if policy != "DIRECTION_ALIGNED_V2":
        raise ValueError("INVALID_RANKING_POLICY")

    ranks = {}

    for row in ranking["ranked_rows"]:
        if row.get("ranking_policy") != policy:
            raise ValueError("RANK_POLICY_MISMATCH")
        symbol = row["symbol"]
        rank = row["observed_rank"]

        if (
            symbol not in scanned
            or symbol in ranks
            or type(rank) is not int
            or rank <= 0
            or row.get("coverage_verified") is not False
            or row.get("approved_for_trade") is not False
            or row.get("real_alert_sent") is not False
        ):
            raise ValueError("INVALID_RANK_ROW")

        ranks[symbol] = rank

    if sorted(ranks.values()) != list(
        range(1, len(ranks) + 1)
    ):
        raise ValueError("INVALID_RANK_SEQUENCE")

    previous_time = 0

    for record in history:
        if not isinstance(record, dict):
            raise ValueError("INVALID_HISTORY_RECORD")

        tm = record.get("scan_time")

        if (
            type(tm) is not int
            or not previous_time < tm < scan_time
        ):
            raise ValueError("RANK_TIME_CONFLICT")

        previous_time = tm

    snapshot = {
        "scan_time": scan_time,
        "scanned_symbols": scanned,
        "ranking_policy": policy,
        "ranks": ranks,
        "status": "MONITOR_ONLY",
    }

    updated = (history + [snapshot])[-5:]

    momentum = {}

    for symbol, rank in ranks.items():
        recent = updated[-4:]

        positions = [
            r.get("ranks", {}).get(symbol)
            for r in recent
        ]

        comparable = (
            len(recent) == 4
            and all(
                r.get("ranking_policy") == policy
                for r in recent
            )
            and all(
                r.get("scanned_symbols") == scanned
                for r in recent
            )
            and all(
                set(r.get("ranks", {})) == set(ranks)
                for r in recent
            )
            and all(
                type(v) is int and v > 0
                for v in positions
            )
        )

        improvements = None
        net_gain = None
        confirmed = False

        if comparable:
            improvements = sum(
                a > b
                for a, b in zip(
                    positions, positions[1:]
                )
            )

            net_gain = positions[0] - positions[-1]

            confirmed = (
                improvements >= 2
                and net_gain >= 3
            )

        momentum[symbol] = {
            "rank": rank,
            "positions": positions,
            "improvements": improvements,
            "net_gain": net_gain,
            "momentum_observed": confirmed,
            "comparable": comparable,
            "status": "MONITOR_ONLY",
            "approved_for_trade": False,
            "real_alert_sent": False,
        }

    # V3_COMMON_RANK_MOMENTUM_CONNECTION_V1
    from kian_v3_common_rank_momentum_v1 import (
        evaluate_common_rank_momentum
    )

    common_momentum = evaluate_common_rank_momentum(
        updated
    )

    if (
        common_momentum.get("status")
        != "MONITOR_ONLY"
        or common_momentum.get("coverage_verified")
        is not False
        or common_momentum.get("approved_for_trade")
        is not False
        or common_momentum.get("real_alert_sent")
        is not False
    ):
        raise RuntimeError(
            "UNSAFE_COMMON_RANK_MOMENTUM"
        )

    return {
        "history": updated,
        "momentum": momentum,
        "common_rank_momentum": common_momentum,
        "status": "MONITOR_ONLY",
        "approved_for_trade": False,
        "real_alert_sent": False,
    }
