import sys
from pathlib import Path

LAB = Path.home() / "kian_v3_upgrade_lab"
BASE = LAB / "v3_main_integration"
STAGE = LAB / "kian_v3_production_stage"

sys.path.insert(0, str(BASE))
sys.path.insert(0, str(STAGE))

from kian_v3_guarded_entry import prepare_guarded_entry


def filter_persistent_observations(observations):
    eligible, blocked = prepare_guarded_entry()

    if not isinstance(observations, list):
        raise ValueError("INVALID_OBSERVATIONS")

    accepted = []
    rejected = []

    for row in observations:
        if not isinstance(row, dict):
            raise ValueError("INVALID_OBSERVATION")

        symbol = row.get("symbol")
        anchor_time = row.get("anchor_time")

        if (
            not isinstance(symbol, str)
            or type(anchor_time) is not int
        ):
            raise ValueError("INVALID_WAVE_ID_FIELDS")

        wave_id = f"{symbol}:{anchor_time}"

        if wave_id in eligible:
            accepted.append(row)
        else:
            rejected.append({
                "wave_id": wave_id,
                "reason": blocked.get(
                    wave_id, "UNKNOWN_OR_UNAUTHORIZED_WAVE"
                ),
            })

    return accepted, rejected


if __name__ == "__main__":
    eligible, blocked = prepare_guarded_entry()

    accepted, rejected = filter_persistent_observations([])

    if accepted or rejected:
        raise RuntimeError("EMPTY_INPUT_NOT_EMPTY")

    if len(eligible) != 366 or len(blocked) != 233:
        raise RuntimeError("UNEXPECTED_GUARD_COUNTS")

    print("=== V3 GUARDED RESUME ===")
    print("AUTHORIZED_MONITOR_WAVES:", len(eligible))
    print("BLOCKED_HISTORICAL_WAVES:", len(blocked))
    print("OBSERVATIONS_APPLIED:", 0)
    print("ENGINE_EXECUTED:", False)
    print("REAL_ALERT_SENT:", False)
    print("MODE: LAB_GUARDED_RESUME")
    print("NO_PRODUCTION_CHANGES")


def execute_guarded_resume(tracker_path, observations):
    import json
    import math
    from kian_v3_incremental_tracker_v4 import IncrementalTracker

    path = Path(tracker_path).resolve()
    safe_root = BASE.resolve()

    if safe_root not in path.parents:
        raise ValueError("UNSAFE_TRACKER_PATH")

    if path.is_symlink() or not path.is_file():
        raise ValueError("INVALID_TRACKER_FILE")

    accepted, rejected = filter_persistent_observations(
        observations
    )

    state = json.loads(path.read_text())

    if (
        state.get("version") != 4
        or not isinstance(state.get("waves"), dict)
    ):
        raise ValueError("INVALID_TRACKER_STATE")

    batch = []

    for row in accepted:
        wave_id = f'{row["symbol"]}:{row["anchor_time"]}'
        wave = state["waves"].get(wave_id)

        if wave is None:
            raise ValueError("AUTHORIZED_WAVE_MISSING")

        timestamp = row.get("time")
        price = row.get("price")

        if (
            type(timestamp) is not int
            or timestamp <= 0
            or type(price) not in (int, float)
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("INVALID_MARKET_OBSERVATION")

        # Historical replay is prohibited.
        if timestamp < wave["last_time"]:
            raise ValueError("STALE_HISTORICAL_OBSERVATION")

        batch.append({
            "wave_id": wave_id,
            "time": timestamp,
            "price": float(price),
        })

    # This lab entry intentionally rejects fresh updates.
    # Historical continuity is not yet verified.
    if any(
        item["time"] > state["waves"][item["wave_id"]]["last_time"]
        for item in batch
    ):
        raise ValueError("FRESH_UPDATES_NOT_YET_AUTHORIZED")

    tracker = IncrementalTracker(path)
    outcomes = tracker.update_batch(batch)

    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "outcomes": outcomes,
        "real_alert_sent": False,
        "mode": "LAB_GUARDED_RESUME",
    }


# V3_UNAMBIGUOUS_WAVE_SELECTOR_V1
def select_unambiguous_waves(scan_rows):
    from collections import defaultdict

    if not isinstance(scan_rows, list):
        raise ValueError("INVALID_SCAN_ROWS")

    eligible, blocked = prepare_guarded_entry()

    by_symbol = defaultdict(list)

    for wave_id, wave in eligible.items():
        symbol = wave.get("symbol")
        if isinstance(symbol, str):
            by_symbol[symbol].append(wave_id)

    selected = []
    skipped = []

    seen_symbols = set()

    for row in scan_rows:
        if not isinstance(row, dict):
            continue

        symbol = row.get("symbol")

        if not isinstance(symbol, str):
            continue

        if symbol in seen_symbols:
            continue

        seen_symbols.add(symbol)

        wave_ids = by_symbol.get(symbol, [])

        if len(wave_ids) != 1:
            skipped.append({
                "symbol": symbol,
                "reason": (
                    "AMBIGUOUS_MULTIPLE_WAVES"
                    if len(wave_ids) > 1
                    else "NO_AUTHORIZED_WAVE"
                ),
            })
            continue

        selected.append({
            "symbol": symbol,
            "wave_id": wave_ids[0],
            "status": "SELECTION_ONLY",
        })

    return {
        "selected": selected,
        "skipped": skipped,
        "mode": "MONITOR_ONLY",
        "real_alert_sent": False,
    }



# V3_FRESH_OBSERVATION_PREPARER_V1
def prepare_fresh_observations(scan_rows, scan_time):
    import math

    if not isinstance(scan_rows, list):
        raise ValueError("INVALID_SCAN_ROWS")

    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")

    selection = select_unambiguous_waves(scan_rows)

    selected = {
        item["symbol"]: item["wave_id"]
        for item in selection["selected"]
    }

    # V3_DUPLICATE_SYMBOL_GUARD_V1
    from collections import Counter

    symbol_counts = Counter(
        row.get("symbol")
        for row in scan_rows
        if isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
    )

    observations = []
    skipped = []

    excluded = {
        "BTCIRT", "XRPIRT", "ADAIRT",
        "SOLIRT", "USDTIRT",
    }

    for row in scan_rows:
        if not isinstance(row, dict):
            continue

        symbol = row.get("symbol")

        if (
            not isinstance(symbol, str)
            or not symbol.endswith("IRT")
            or symbol in excluded
            or symbol not in selected
        ):
            continue

        if symbol_counts[symbol] != 1:
            skipped.append({
                "symbol": symbol,
                "reason": "DUPLICATE_OR_CONFLICTING_SYMBOL",
            })
            continue

        price = row.get("last_trade_price")
        timestamp = row.get("last_trade_time")

        if (
            type(price) not in (int, float)
            or not math.isfinite(price)
            or price <= 0
            or type(timestamp) is not int
            or timestamp <= 0
        ):
            skipped.append({
                "symbol": symbol,
                "reason": "INVALID_TRADE_PAIR",
            })
            continue

        age = scan_time - timestamp

        if age < 0 or age > 15 * 60 * 1000:
            skipped.append({
                "symbol": symbol,
                "reason": "STALE_OR_FUTURE_TRADE",
            })
            continue

        wave_id = selected[symbol]
        anchor_time = int(wave_id.rsplit(":", 1)[1])

        if timestamp < anchor_time:
            skipped.append({
                "symbol": symbol,
                "reason": "TRADE_BEFORE_WAVE",
            })
            continue

        observations.append({
            "symbol": symbol,
            "anchor_time": anchor_time,
            "wave_id": wave_id,
            "time": timestamp,
            "price": float(price),
        })

    return {
        "observations": observations,
        "skipped": skipped,
        "selection_skipped": selection["skipped"],
        "mode": "PREPARE_ONLY",
        "real_alert_sent": False,
    }



# V3_PERSISTENT_PREVIEW_V1
def preview_persistent_resume(tracker_path, observations):
    import json
    import math

    path = Path(tracker_path).resolve()
    safe_root = BASE.resolve()

    if safe_root not in path.parents:
        raise ValueError("UNSAFE_TRACKER_PATH")

    if not path.is_file():
        raise ValueError("INVALID_TRACKER_FILE")

    if not isinstance(observations, list):
        raise ValueError("INVALID_OBSERVATIONS")

    state = json.loads(path.read_text())

    if (
        state.get("version") != 4
        or not isinstance(state.get("waves"), dict)
    ):
        raise ValueError("INVALID_TRACKER_STATE")

    eligible, blocked = prepare_guarded_entry()

    ready = []
    skipped = []
    seen = set()

    for row in observations:
        if not isinstance(row, dict):
            continue

        wave_id = row.get("wave_id")
        timestamp = row.get("time")
        price = row.get("price")

        if (
            not isinstance(wave_id, str)
            or wave_id not in eligible
            or wave_id in blocked
            or wave_id not in state["waves"]
        ):
            skipped.append("UNAUTHORIZED_WAVE")
            continue

        if (
            type(timestamp) is not int
            or type(price) not in (int, float)
            or not math.isfinite(price)
            or price <= 0
        ):
            skipped.append("INVALID_OBSERVATION")
            continue

        if wave_id in seen:
            skipped.append("DUPLICATE_WAVE")
            continue

        seen.add(wave_id)
        wave = state["waves"][wave_id]

        if timestamp <= wave["last_time"]:
            skipped.append("NOT_NEWER_THAN_TRACKER")
            continue

        ready.append({
            "wave_id": wave_id,
            "time": timestamp,
            "price": float(price),
            "previous_time": wave["last_time"],
            "status": "PREVIEW_ONLY",
        })

    return {
        "ready_count": len(ready),
        "skipped_count": len(skipped),
        "ready": ready,
        "skipped": skipped,
        "mode": "PREVIEW_ONLY",
        "tracker_updated": False,
        "real_alert_sent": False,
    }

