import math

import tempfile
from pathlib import Path

from kian_v3_feature_bridge_v1 import build_safe_feature_bridge
from kian_v3_upgrade_engine import KianV3UpgradeEngine
from kian_v3_unified_safe_runner import run_unified_safe
from kian_v3_pipeline_v1 import build_safe_pipeline



def _v3_safe_state_root(lab_state_dir):
    root = Path(lab_state_dir).expanduser().resolve()
    lab = (Path.home() / "kian_v3_upgrade_lab").resolve()

    if root != lab and lab in root.parents:
        return root

    from kian_v3_state_config import (
        V3_MODE, V3_PRODUCTION_APPROVED
    )

    from kian_v3_state_config import get_v3_state_dir
    production = (
        Path(__file__).resolve().parent / "v3_state"
    ).resolve()

    # V3_PORTABLE_MONITOR_STATE_GUARD_V1
    # Allow only the dedicated state directory in monitor mode.
    # This does not authorize production alerts or trading.
    if (
        root == production
        and V3_MODE == "MONITOR_ONLY"
        and V3_PRODUCTION_APPROVED is False
    ):
        return root

    # V3_PRIVATE_RUNNER_TEMP_GUARD
    # Permit an isolated GitHub runner state directory in monitor mode.
    import os
    runner_temp = os.environ.get("RUNNER_TEMP")
    if (
        runner_temp
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and V3_MODE == "MONITOR_ONLY"
        and V3_PRODUCTION_APPROVED is False
    ):
        temp_root = Path(runner_temp).expanduser().resolve()
        if root != temp_root and root.is_relative_to(temp_root):
            return root

    raise ValueError("UNSAFE_V3_STATE_PATH")


def run_integrated_lab(
    symbol, anchor_time, cutoff, fast, radar,
    allowed_irt_symbols,
    lab_state_dir=None,
):
    features = build_safe_feature_bridge(
        symbol, anchor_time, cutoff, fast, radar
    )

    from math import isfinite

    eligible = [
        r for r in fast
        if isinstance(r, dict)
        and r.get("symbol") == symbol
        and r.get("anchor_time") == anchor_time
        and type(r.get("time")) is int
        and anchor_time <= r["time"] <= cutoff
        and cutoff - r["time"] <= 1800000
        and type(r.get("price")) in (int, float)
        and isfinite(r["price"])
        and r["price"] > 0
    ]

    if not eligible:
        raise ValueError("VALID_FAST_PRICE_REQUIRED")

    selected = max(eligible, key=lambda r: r["time"])

    fast_row = {
        "symbol": symbol,
        "time": selected["time"],
        "price": selected["price"],
        "1m": features["fast"]["1m"],
        "3m": features["fast"]["3m"],
        "5m": features["fast"]["5m"],
    }

    radar_features = features["radar"]

    score_row = {
        "symbol": symbol,
        "status": "INSUFFICIENT_DATA",
        "p15": radar_features.get("p15"),
        "va": radar_features.get("va"),
        "ta": radar_features.get("ta"),
        "bs": radar_features.get("bs"),
        "book": radar_features.get("book"),
        "vr": radar_features.get("vr"),
        "breakout": radar_features.get("breakout"),
        "score": radar_features.get("hunt_score"),
        "structure1h": radar_features.get("structure1h"),
        "structure4h": radar_features.get("structure4h"),
        "compression15": radar_features.get("compression15"),
    }

    from contextlib import nullcontext

    if lab_state_dir is None:
        state_context = tempfile.TemporaryDirectory(
            prefix="kian_v3_integrated_"
        )
    else:
        safe_base = (
            Path.home() / "kian_v3_upgrade_lab"
        ).resolve()
        root_candidate = Path(lab_state_dir).expanduser().resolve()

        if (
            root_candidate == safe_base
            or safe_base not in root_candidate.parents
        ):
            raise ValueError("UNSAFE_LAB_STATE_PATH")

        root_candidate.mkdir(parents=True, exist_ok=True)
        state_context = nullcontext(str(root_candidate))

    with state_context as tmp:
        root = Path(tmp)

        engine = KianV3UpgradeEngine(
            state_path=root / "tracker.json",
            unified_state_path=root / "unified.json",
        )

        if engine.production_enabled or engine.real_alerts_enabled:
            raise RuntimeError("UNSAFE_ENGINE_CONFIGURATION")

        # Persist valid FAST observations in chronological order.
        # Only the isolated lab store is used; no production alerts.
        from kian_v3_unified_store import UnifiedWaveStore
        fast_store = UnifiedWaveStore(root / "unified.json")
        chronological = sorted(
            (
                r for r in fast
                if isinstance(r, dict)
                and r.get("symbol") == symbol
                and r.get("anchor_time") == anchor_time
                and type(r.get("time")) is int
                and anchor_time <= r["time"] <= cutoff
                and cutoff - r["time"] <= 1800000
                and type(r.get("price")) in (int, float)
                and math.isfinite(r["price"])
                and r["price"] > 0
            ),
            key=lambda r: r["time"]
        )
        for observation in chronological:
            fast_store.update(f"{symbol}:{anchor_time}", observation)

        result = run_unified_safe(
            engine=engine,
            symbol=symbol,
            anchor_time=anchor_time,
            row=fast_row,
            score_row=score_row,
            now_ms=cutoff,
            cutoff_ms=cutoff,
        )

        ranking = build_safe_pipeline(
            [result], allowed_irt_symbols
        )

        import json

        tracker_path = root / "tracker.json"
        tracker_state = (
            json.loads(tracker_path.read_text(encoding="utf-8"))
            if tracker_path.exists()
            else {"version": 4, "waves": {}}
        )

        wave_id = f"{symbol}:{anchor_time}"
        wave_state = tracker_state.get("waves", {}).get(wave_id)

        reignition_status = "NO_ACTIVE_CORRECTION"
        if isinstance(wave_state, dict):
            correction = wave_state.get("open_correction")
            if isinstance(correction, dict):
                low = correction.get("low")
                prior_peak = correction.get("prior_peak")
                last_price = wave_state.get("last_price")

                if (
                    type(low) in (int, float)
                    and type(prior_peak) in (int, float)
                    and type(last_price) in (int, float)
                    and all(
                        math.isfinite(v)
                        for v in (low, prior_peak, last_price)
                    )
                    and low > 0
                    and prior_peak > low
                    and last_price <= prior_peak
                    and 100 * (last_price / low - 1) >= 5.0
                ):
                    reignition_status = "REIGNITION_WATCH"
                else:
                    reignition_status = "CORRECTION_OPEN"

        fast_1m = features["fast"]["1m"]

        unified_path = root / "unified.json"
        unified_state = (
            json.loads(unified_path.read_text(encoding="utf-8"))
            if unified_path.exists()
            else {"waves": {}}
        )

        unified_wave = unified_state.get("waves", {}).get(wave_id, {})
        stored_history = unified_wave.get("fast_history", [])
        if not isinstance(stored_history, list):
            stored_history = []

        current_fast_time = selected["time"]
        previous_fast = None

        candidates = [
            r for r in [*stored_history, *fast]
            if isinstance(r, dict)
            and type(r.get("time")) is int
            and anchor_time <= r["time"]
            and 60000 <= current_fast_time - r["time"] <= 600000
            and (
                r in stored_history
                or (
                    r.get("symbol") == symbol
                    and r.get("anchor_time") == anchor_time
                )
            )
        ]

        if candidates:
            previous_fast = max(
                candidates, key=lambda r: r["time"]
            )

        def valid_positive(value):
            import math
            return (
                type(value) in (int, float)
                and math.isfinite(value)
                and value > 0
            )

        fast_confirmation = False
        fast_acceleration = False
        fast_evidence_status = "INSUFFICIENT_DATA"

        if reignition_status == "REIGNITION_WATCH":
            current_trades = fast_1m.get("trades")
            current_volume = fast_1m.get("volume")
            bp = fast_1m.get("buy_pressure")
            pm = fast_1m.get("price_move")

            baseline = (
                previous_fast.get("1m")
                if isinstance(previous_fast, dict)
                else None
            )
            baseline = baseline if isinstance(baseline, dict) else {}

            previous_trades = baseline.get("trades")
            previous_volume = baseline.get("volume")

            if all(valid_positive(x) for x in (
                current_trades, current_volume,
                previous_trades, previous_volume,
            )):
                fast_acceleration = (
                    current_trades >= 1.5 * previous_trades
                    and current_volume >= 1.5 * previous_volume
                )

                if fast_acceleration:
                    fast_evidence_status = "FAST_ACCELERATION_WATCH"

                    if valid_positive(bp) and valid_positive(pm):
                        # V3_REIGNITION_FAST_TRUST_GUARD_V1
                        # Observed acceleration is not verified trade coverage.
                        fast_confirmation = False
                        fast_evidence_status = "FAST_ACCELERATION_UNVERIFIED"

        if fast_confirmation:
            reignition_status = "EARLY_REIGNITION_CANDIDATE"

        fast_monitor = {
            "symbol": symbol,
            "wave_id": wave_id,
            "evidence_status": fast_evidence_status,
            "acceleration": fast_acceleration,
            "confirmed": fast_confirmation,
            "reignition_status": reignition_status,
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
        }

        ranking["fast_monitor"] = fast_monitor

        early_context = result.get("early_pump_context")
        if not isinstance(early_context, dict):
            raise RuntimeError("INVALID_EARLY_CONTEXT")

        if (
            early_context.get("final_decision", {}).get("status")
            != "BLOCKED"
        ):
            raise RuntimeError("UNSAFE_EARLY_CONTEXT")

        early_context["fast_monitor"] = dict(fast_monitor)
        early_context["evidence_status"] = (
            "UNVERIFIED_MONITOR_ONLY"
        )

        saved_wave = (
            wave_state if isinstance(wave_state, dict) else {}
        )
        correction = saved_wave.get("open_correction")
        episodes = saved_wave.get("reignitions")

        if not isinstance(episodes, list):
            episodes = []

        reignition_watch = {
            "status": "NOT_ACTIVE",
            "rebound_from_low_pct": None,
            "prior_peak": None,
            "correction_low": None,
            "requires_fast_confirmation": True,
            "real_alert_sent": False,
        }

        if isinstance(correction, dict):
            low = correction.get("low")
            prior_peak = correction.get("prior_peak")
            last_price = saved_wave.get("last_price")

            if all(
                type(v) in (int, float)
                and math.isfinite(v)
                and v > 0
                for v in (low, prior_peak, last_price)
            ):
                rebound = 100 * (last_price / low - 1)
                reignition_watch.update({
                    "rebound_from_low_pct": round(rebound, 3),
                    "prior_peak": prior_peak,
                    "correction_low": low,
                })
                if (
                    rebound >= 5.0
                    and last_price <= prior_peak
                ):
                    reignition_watch["status"] = "REIGNITION_WATCH"

        wave_monitor = {
            "reignition_watch": reignition_watch,
            "wave_id": wave_id,
            "state_available": isinstance(wave_state, dict),
            "last_time": saved_wave.get("last_time"),
            "last_price": saved_wave.get("last_price"),
            "peak_price": saved_wave.get("peak_price"),
            "correction_open": isinstance(correction, dict),
            "reignition_count": len(episodes),
            "reignition_status": reignition_status,
            "fast_evidence_status": fast_evidence_status,
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
        }

        ranking["wave_monitor"] = wave_monitor
        early_context["wave_monitor"] = dict(wave_monitor)

        return {
            "features": features,
            "engine_result": result,
            "ranking": ranking,
            "wave_id": wave_id,
            "wave_state": wave_state,
            "reignition_status": reignition_status,
            "fast_confirmation": fast_confirmation,
            "fast_acceleration": fast_acceleration,
            "fast_evidence_status": fast_evidence_status,
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
        }


def resume_saved_waves_for_scan(
    scan_time, fresh_observations,
    allowed_irt_symbols, lab_state_dir
):
    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")

    if not isinstance(fresh_observations, list):
        raise ValueError("INVALID_FRESH_OBSERVATIONS")

    root = _v3_safe_state_root(lab_state_dir)

    root.mkdir(parents=True, exist_ok=True)

    engine = KianV3UpgradeEngine(
        state_path=root / "tracker.json",
        unified_state_path=root / "unified.json",
    )

    if engine.production_enabled or engine.real_alerts_enabled:
        raise RuntimeError("UNSAFE_ENGINE_CONFIGURATION")

    for row in fresh_observations:
        if not isinstance(row, dict):
            raise ValueError("INVALID_SCAN_OBSERVATION")
        if type(row.get("time")) is not int:
            raise ValueError("INVALID_OBSERVATION_TIME")
        if row["time"] > scan_time:
            raise ValueError("FUTURE_OBSERVATION_REJECTED")

    result = engine.resume_persistent_waves(
        allowed_irt_symbols,
        fresh_observations,
    )

    if result.get("status") != "MONITOR_ONLY":
        raise RuntimeError("UNSAFE_RESUME_STATUS")

    if result.get("real_alert_sent") is not False:
        raise RuntimeError("UNSAFE_ALERT_STATUS")

    return {
        "scan_time": scan_time,
        "pending_waves": result["pending_waves"],
        "fresh_matches": result["fresh_matches"],
        "applied_count": result["applied_count"],
        "observations": result["observations"],
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }


def resume_saved_waves_from_market_rows(
    scan_time, market_rows,
    allowed_irt_symbols, lab_state_dir
):
    import math

    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")

    if not isinstance(market_rows, list):
        raise ValueError("INVALID_MARKET_ROWS")

    root = _v3_safe_state_root(lab_state_dir)

    engine = KianV3UpgradeEngine(
        state_path=root / "tracker.json",
        unified_state_path=root / "unified.json",
    )

    pending = engine.list_persistent_waves(
        allowed_irt_symbols
    )

    by_symbol = {}
    for wave in pending:
        by_symbol.setdefault(wave["symbol"], []).append(wave)

    observations = []
    seen_market = {}

    for row in market_rows:
        if not isinstance(row, dict):
            raise ValueError("INVALID_MARKET_ROW")

        symbol = row.get("symbol")
        timestamp = row.get("time")
        price = row.get("price")

        if (
            not isinstance(symbol, str)
            or type(timestamp) is not int
            or type(price) not in (int, float)
            or not math.isfinite(price)
            or price <= 0
            or timestamp <= 0
            or timestamp > scan_time
        ):
            raise ValueError("INVALID_MARKET_OBSERVATION")

        key = (symbol, timestamp)
        if key in seen_market:
            if seen_market[key] != float(price):
                raise ValueError("MARKET_PRICE_CONFLICT")
            continue

        seen_market[key] = float(price)

        for wave in by_symbol.get(symbol, []):
            if timestamp <= wave["last_time"]:
                continue

            observations.append({
                "symbol": symbol,
                "anchor_time": wave["anchor_time"],
                "time": timestamp,
                "price": float(price),
            })

    result = resume_saved_waves_for_scan(
        scan_time=scan_time,
        fresh_observations=observations,
        allowed_irt_symbols=allowed_irt_symbols,
        lab_state_dir=root,
    )

    return {
        **result,
        "market_rows_received": len(market_rows),
        "wave_observations_matched": len(observations),
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }


def resume_saved_waves_from_trades(
    scan_time, allowed_irt_symbols, lab_state_dir,
    max_symbols=5
):
    import fcntl
    import json
    import os
    import tempfile
    from kian_v3_trade_collector import fetch_trades

    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")
    if type(max_symbols) is not int or not 1 <= max_symbols <= 5:
        raise ValueError("INVALID_SYMBOL_BUDGET")

    root = _v3_safe_state_root(lab_state_dir)

    root.mkdir(parents=True, exist_ok=True)

    engine = KianV3UpgradeEngine(
        state_path=root / "tracker.json",
        unified_state_path=root / "unified.json",
    )

    pending = engine.list_persistent_waves(allowed_irt_symbols)
    symbols = sorted({wave["symbol"] for wave in pending})

    root.mkdir(parents=True, exist_ok=True)
    cursor_path = root / "fair_scan_cursor.json"
    lock_path = root / "fair_scan_cursor.lock"

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if cursor_path.exists():
                cursor = json.loads(cursor_path.read_text())
                if cursor.get("version") != 1:
                    raise ValueError("INVALID_CURSOR_VERSION")
                last_symbol = cursor.get("last_symbol")
            else:
                last_symbol = None

            if last_symbol in symbols:
                start_index = (symbols.index(last_symbol) + 1) % len(symbols)
            else:
                start_index = 0

            selected = (
                (symbols[start_index:] + symbols[:start_index])[:max_symbols]
                if symbols else []
            )

            market_rows = []
            trade_data_status = {}
            for symbol in selected:
                trades = fetch_trades(symbol, limit=1000)
                if not trades:
                    trade_data_status[symbol] = "NO_TRADES"
                    continue
                latest_by_time = {}
                trade_ids = {}
                for trade in trades:
                    timestamp = trade["time"]
                    trade_id = trade["id"]
                    if timestamp > scan_time:
                        continue
                    if trade_id in trade_ids and trade_ids[trade_id] != trade:
                        raise ValueError("TRADE_ID_CONFLICT")
                    trade_ids[trade_id] = trade
                    previous = latest_by_time.get(timestamp)
                    if previous is None or trade_id > previous["id"]:
                        latest_by_time[timestamp] = trade

                if not latest_by_time:
                    trade_data_status[symbol] = "NO_VALID_TRADES"
                    continue
                newest_time = max(latest_by_time)
                if scan_time - newest_time > 1800000:
                    trade_data_status[symbol] = "STALE_DATA"
                    continue
                trade_data_status[symbol] = "FRESH_DATA"

                for timestamp, trade in sorted(latest_by_time.items()):
                    market_rows.append({
                        "symbol": symbol,
                        "time": timestamp,
                        "price": trade["price"],
                    })

            result = resume_saved_waves_from_market_rows(
                scan_time=scan_time,
                market_rows=market_rows,
                allowed_irt_symbols=allowed_irt_symbols,
                lab_state_dir=root,
            )

            if selected:
                next_cursor = {
                    "version": 1,
                    "last_symbol": selected[-1],
                    "last_scan_time": scan_time,
                }
                fd, temp_name = tempfile.mkstemp(
                    prefix=".fair_cursor_",
                    dir=root,
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(next_cursor, f)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_name, cursor_path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)

            return {
                **result,
                "symbols_checked": selected,
                "symbol_budget": max_symbols,
                "fair_rotation": True,
                "trade_data_status": trade_data_status,
                "status": "MONITOR_ONLY",
                "real_alert_sent": False,
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_comprehensive_v3_scan(
    scan_time, candidates, allowed_irt_symbols,
    lab_state_dir, max_persistent_symbols=5,
    fetch_persistent_trades=False,
    verified_market_rows=None, persistent_market_rows=None,
    verified_wave_observations=None
):
    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")

    if not isinstance(candidates, list):
        raise ValueError("INVALID_CANDIDATES")

    if not isinstance(allowed_irt_symbols, (set, frozenset)):
        raise ValueError("INVALID_MARKET_UNIVERSE")

    if verified_market_rows is not None:
        raise ValueError(
            "MARKET_SOURCE_VERIFICATION_NOT_CONNECTED"
        )

    root = _v3_safe_state_root(lab_state_dir)

    from kian_v3_wave_context import load_wave_context

    historical_wave_context = load_wave_context(
        root,
        allowed_irt_symbols,
    )


    # Load historical evidence once per scan, strictly for reporting.
    historical_by_wave = {}
    historical_file = (
        Path(lab_state_dir) / "fast_wake_evidence.json"
    )
    if historical_file.is_file():
        import json
        historical_data = json.loads(
            historical_file.read_text(encoding="utf-8")
        )
        if (
            isinstance(historical_data, dict)
            and historical_data.get("mode")
            == "ISOLATED_HISTORICAL_EVIDENCE"
        ):
            for row in historical_data.get("records", []):
                if not isinstance(row, dict):
                    continue
                key = (row.get("symbol"), row.get("anchor_time"))
                historical_by_wave.setdefault(key, []).append(row)

    candidate_results = []
    processed_waves = set()

    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("INVALID_CANDIDATE")

        symbol = item.get("symbol")
        anchor_time = item.get("anchor_time")

        if (
            symbol not in allowed_irt_symbols
            or type(anchor_time) is not int
            or anchor_time <= 0
        ):
            continue

        wave_id = f"{symbol}:{anchor_time}"
        if wave_id in processed_waves:
            continue

        processed_waves.add(wave_id)

        from math import isfinite

        # FAST_SOURCE_GUARD_V1
        # Self-declared FAST_VERIFIED is not trusted.
        # Keep unverified FAST strictly in monitor-only reporting.
        fast_monitor_rows = [
            row for row in (
                item.get("fast", []) + item.get("fast_observed", [])
            )
            if isinstance(row, dict)
            and row.get("symbol") == symbol
            and row.get("anchor_time") == anchor_time
            and row.get("evidence_status") in (
                "FAST_SOURCE_PENDING", "FAST_OBSERVED"
            )
            and row.get("coverage_verified") is False
        ]

        valid_fast = [
            row for row in item.get("fast", [])
            if isinstance(row, dict)
            and False  # Trusted FAST source not yet connected
            and row.get("coverage_verified") is True
            and row.get("evidence_status") == "FAST_VERIFIED"
            and row.get("symbol") == symbol
            and row.get("anchor_time") == anchor_time
            and type(row.get("time")) is int
            and anchor_time <= row["time"] <= scan_time
            and scan_time - row["time"] <= 1800000
            and type(row.get("price")) in (int, float)
            and isfinite(row["price"])
            and row["price"] > 0
        ]

        if not valid_fast:
            from kian_v3_early_watch import observe_early_watch

            radar_data = item.get("radar", {})
            radar_rows = (
                radar_data.get(symbol, [])
                if isinstance(radar_data, dict)
                else []
            )
            current = radar_rows[-1] if radar_rows else {}
            watch_status = "VALID_RADAR_PRICE_REQUIRED"

            if (
                isinstance(current, dict)
                and type(current.get("time")) is int
                and anchor_time <= current["time"] <= scan_time
                and scan_time - current["time"] <= 300000
                and type(current.get("price")) in (int, float)
                and isfinite(current["price"])
                and current["price"] > 0
            ):
                watch_status = observe_early_watch(
                    Path(lab_state_dir) / "early_watch.json",
                    symbol,
                    anchor_time,
                    current["time"],
                    current["price"],
                    item.get("anchor_price"),
                )

                if watch_status in ("EARLY_WATCH_SAVED", "DUPLICATE"):
                    from kian_v3_early_watch import record_observed_fast
                    record_observed_fast(
                        Path(lab_state_dir) / "early_watch.json",
                        symbol,
                        anchor_time,
                        item.get("fast_observed", []),
                    )

            result = {
                "early_watch_status": watch_status,
                "symbol": symbol,
                "wave_id": wave_id,
                "features": None,
                "engine_result": {
                    "preliminary": {
                        "classification": {
                            "status": "FAST_UNVERIFIED"
                        }
                    },
                    "final_decision": {
                        "status": "BLOCKED",
                        "reason": "VALID_FAST_PRICE_REQUIRED",
                    },
                },
                "ranking": {},
                "wave_state": None,
                "reignition_status": "UNVERIFIED",
                "fast_evidence_status": "FAST_UNVERIFIED",
                "fast_monitor_count": len(fast_monitor_rows),
                "status": "MONITOR_ONLY",
                "real_alert_sent": False,
            }
        else:
            result = run_integrated_lab(
                symbol=symbol,
                anchor_time=anchor_time,
                cutoff=scan_time,
                fast=valid_fast,
                radar=item.get("radar", []),
                allowed_irt_symbols=allowed_irt_symbols,
                lab_state_dir=lab_state_dir,
            )

            if not isinstance(result, dict):
                raise RuntimeError("INVALID_INTEGRATED_RESULT")

            if result.get("symbol", symbol) != symbol:
                raise RuntimeError("INTEGRATED_SYMBOL_MISMATCH")

            if result.get("wave_id", wave_id) != wave_id:
                raise RuntimeError("INTEGRATED_WAVE_ID_MISMATCH")

            result["symbol"] = symbol
            result["wave_id"] = wave_id

        if result.get("real_alert_sent") is not False:
            raise RuntimeError("UNSAFE_CANDIDATE_ALERT")

        from kian_v3_state_machine_v1 import WaveMachine

        machine = WaveMachine()
        valid_rows = sorted(
            (
                row for row in item.get("fast", [])
                if isinstance(row, dict)
                and False  # V3_SECONDARY_FAST_GUARD
                and row.get("coverage_verified") is True
                and row.get("evidence_status") == "FAST_VERIFIED"
                and row.get("symbol") == symbol
                and row.get("anchor_time") == anchor_time
                and type(row.get("time")) is int
                and anchor_time <= row["time"] <= scan_time
            ),
            key=lambda row: row["time"],
        )

        for row in valid_rows:
            machine.update(row)

        result["fast_state_machine"] = {
            "state": machine.state,
            "ignition_seen": machine.ignition_seen,
            "transitions": list(machine.transitions),
            "last_time": machine.last_time,
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
        }

        ranking = result.get("ranking") or {}
        engine_result = result.get("engine_result") or {}
        preliminary = engine_result.get("preliminary") or {}
        classification = preliminary.get("classification") or {}
        final_decision = engine_result.get("final_decision") or {}
        wave_monitor = ranking.get("wave_monitor") or {}
        fast_state = result.get("fast_state_machine") or {}

        # Compare persisted FAST state without changing it.
        persisted_fast_state = "UNVERIFIED"
        persisted_ignition_seen = None
        state_file = Path(lab_state_dir).expanduser().resolve() / "unified.json"
        if state_file.exists():
            import json
            import fcntl
            state_lock = Path(str(state_file) + ".lock")
            with state_lock.open("a+b") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_SH)
                try:
                    state_data = json.loads(state_file.read_text())
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
            stored_wave = state_data.get("waves", {}).get(wave_id, {})
            if isinstance(stored_wave, dict):
                persisted_fast_state = stored_wave.get("fast_state", "UNVERIFIED")
                persisted_ignition_seen = stored_wave.get("ignition_seen")
        computed_fast_state = fast_state.get("state", "UNKNOWN")
        effective_fast_state = (
            persisted_fast_state
            if persisted_fast_state in {
                "QUIET_WATCH", "ACTIVITY_DETECTED",
                "IGNITION_CANDIDATE", "REIGNITION_CANDIDATE"
            }
            else "UNVERIFIED"
        )
        if effective_fast_state == "UNVERIFIED":
            state_comparison = "UNVERIFIED"
        elif effective_fast_state == computed_fast_state:
            state_comparison = "MATCH"
        elif (
            effective_fast_state == "REIGNITION_CANDIDATE"
            and computed_fast_state == "IGNITION_CANDIDATE"
        ):
            state_comparison = "PRIORITY_DIFFERENCE"
        else:
            state_comparison = "STATE_MISMATCH"

        # Conservative reignition gate: observation only.
        candle_result = engine_result.get("candle_confirmation") or {}
        market_safety = engine_result.get("market_safety") or {}
        reignition_gate = {
            "status": "BLOCKED",
            "monitoring": "MONITOR_ONLY",
            "real_alert_sent": False,
            "fast_state": effective_fast_state,
        }
        if effective_fast_state != "REIGNITION_CANDIDATE":
            reignition_gate["reason"] = "NO_REIGNITION_ACTIVITY"
        elif candle_result.get("status") != "EVALUATED":
            reignition_gate["reason"] = "CANDLE_CONFIRMATION_NOT_READY"
        elif not all(
            candle_result.get(k) is True
            for k in (
                "breakout_15m_confirmed",
                "trend_1h_confirmed",
                "structure_4h_intact",
            )
        ):
            reignition_gate["reason"] = "MULTI_TIMEFRAME_NOT_CONFIRMED"
        elif market_safety.get("status") != "APPROVED":
            reignition_gate["reason"] = "MARKET_SAFETY_NOT_APPROVED"
        else:
            reignition_gate["reason"] = "AWAITING_INTEGRATED_VALIDATION"

        # Expose existing safety evidence without approving trades.
        safety_evidence = {
            "status": market_safety.get("status", "BLOCKED"),
            "reason": market_safety.get(
                "reason", "SAFETY_NOT_VERIFIED"
            ),
            "liquidity": market_safety.get(
                "liquidity", "UNKNOWN"
            ),
            "late_pump": market_safety.get(
                "late_pump", "UNKNOWN"
            ),
            "spread_pct": market_safety.get("spread_pct"),
            "bid_notional_top10": market_safety.get(
                "bid_notional_top10"
            ),
            "ask_notional_top10": market_safety.get(
                "ask_notional_top10"
            ),
            "gain_from_anchor_pct": market_safety.get(
                "gain_from_anchor_pct"
            ),
            "peak_gain_pct": market_safety.get("peak_gain_pct"),
            "drawdown_from_peak_pct": market_safety.get(
                "drawdown_from_peak_pct"
            ),
            "risk_flags": list(
                market_safety.get("risk_flags") or []
            ),
            "approved_for_trade": False,
            "real_alert_sent": False,
        }

        # Integrated observation only: no trading authorization.
        candle_keys = (
            "breakout_15m_confirmed",
            "trend_1h_confirmed",
            "structure_4h_intact",
        )
        candle_confirmed = (
            candle_result.get("status") == "EVALUATED"
            and all(candle_result.get(k) is True for k in candle_keys)
        )
        fast_active = effective_fast_state in (
            "IGNITION_CANDIDATE",
            "REIGNITION_CANDIDATE",
        )
        safety_approved = market_safety.get("status") == "APPROVED"
        engine_blocked = final_decision.get("status") != "APPROVED"

        if not fast_active:
            observation_status = "WATCH"
            observation_reason = "FAST_NOT_CONFIRMED"
        elif not candle_confirmed:
            observation_status = "CONFIRMATION_PENDING"
            observation_reason = "CANDLE_CONFIRMATION_PENDING"
        elif not safety_approved or engine_blocked:
            observation_status = "SAFETY_BLOCKED"
            observation_reason = "SAFETY_OR_ENGINE_NOT_APPROVED"
        else:
            observation_status = "REVIEW_READY"
            observation_reason = "MANUAL_REVIEW_ONLY"

        integrated_observation = {
            "status": observation_status,
            "reason": observation_reason,
            "effective_fast_state": effective_fast_state,
            "candle_confirmed": candle_confirmed,
            "safety_approved": safety_approved,
            "engine_blocked": engine_blocked,
            "monitoring": "MONITOR_ONLY",
            "approved_for_trade": False,
            "real_alert_sent": False,
        }

        historical_waves = historical_wave_context.get(symbol, [])
        same_wave = next(
            (w for w in historical_waves if w["wave_id"] == wave_id),
            None,
        )

        last_reignition_time = (
            same_wave.get("last_reignition_time")
            if same_wave else None
        )

        move_reignition_confirmed = bool(
            same_wave
            and same_wave.get("verified_reignition_count", 0) > 0
        )

        fresh_move_reignition = bool(
            move_reignition_confirmed
            and type(last_reignition_time) is int
            and 0 <= scan_time - last_reignition_time <= 900000
        )

        persistent_reignition_evidence = {
            "last_reignition_time": last_reignition_time,
            "fresh_move_reignition": fresh_move_reignition,
            "fast_reignition_candidate": (
                effective_fast_state == "REIGNITION_CANDIDATE"
            ),
            "fast_state_comparison": state_comparison,
            "candle_confirmed": candle_confirmed,
            "market_safety_approved": safety_approved,
            "same_historical_wave": same_wave is not None,
            "tracked_correction_open": bool(
                same_wave
                and same_wave.get("correction_status")
                == "TRACKED_CORRECTION"
            ),
            "move_reignition_recorded": move_reignition_confirmed,
            "evidence_status": (
                "FRESH_RECORDED_REIGNITION"
                if fresh_move_reignition
                else "HISTORICAL_REIGNITION_RECORDED"
                if move_reignition_confirmed
                else "TRACKED_CORRECTION_PENDING"
                if same_wave and same_wave.get(
                    "correction_status"
                ) == "TRACKED_CORRECTION"
                else "HISTORICAL_EVIDENCE_INSUFFICIENT"
            ),
            "monitoring": "MONITOR_ONLY",
            "approved_for_trade": False,
            "real_alert_sent": False,
        }

        # Independent, unverified observation priority: report only.
        observed_status = "NOT_AVAILABLE"
        observed_priority = "WAITING"
        observed_path = Path(lab_state_dir) / "early_watch.json"
        if observed_path.exists():
            import json
            import fcntl
            observed_lock = Path(str(observed_path) + ".lock")
            with observed_lock.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                try:
                    observed_data = json.loads(observed_path.read_text())
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            if isinstance(observed_data, dict):
                waves = observed_data.get("waves", {})
                wave = waves.get(f"{symbol}:{anchor_time}") if isinstance(waves, dict) else None
                if isinstance(wave, dict):
                    assessment = wave.get("fast_activity_assessment", {})
                    latest = wave.get("fast_observed", {})
                    latest_time = latest.get("time") if isinstance(latest, dict) else None
                    if (
                        isinstance(assessment, dict)
                        and type(latest_time) is int
                        and 0 <= scan_time - latest_time <= 60000
                        and latest.get("coverage_verified") is False
                        and latest.get("evidence_status") == "FAST_OBSERVED"
                    ):
                        observed_status = assessment.get("status", "UNKNOWN")
                        observed_priority = {
                            "ACTIVITY_RISING_OBSERVED": "HIGH_WATCH",
            "ACTIVITY_SURGE_OBSERVED": "HIGH_WATCH",
            "ACTIVITY_ACCELERATION_OBSERVED": "HIGH_WATCH",
                            "NO_CONSISTENT_RISE": "NORMAL_WATCH",
                            "INSUFFICIENT_HISTORY": "WAITING",
                            "INVALID_HISTORY": "INVALID",
                        }.get(observed_status, "WAITING")
                    else:
                        observed_status = "STALE_OR_UNVERIFIED"

        reignition_activity_watch = {
            "status": (
                "REIGNITION_ACTIVITY_WATCH"
                if (
                    (wave_monitor.get("reignition_watch") or {}).get("status")
                    == "REIGNITION_WATCH"
                    and observed_priority == "HIGH_WATCH"
                    and observed_status in {
                        "ACTIVITY_RISING_OBSERVED",
                        "ACTIVITY_SURGE_OBSERVED",
                        "ACTIVITY_ACCELERATION_OBSERVED",
                    }
                )
                else "NOT_READY"
            ),
            "evidence_status": "UNVERIFIED_MONITOR_ONLY",
            "approved_for_trade": False,
            "real_alert_sent": False,
        }

        result["candidate_report"] = {
            "reignition_activity_watch": reignition_activity_watch,
            "symbol": symbol,
            "persistent_reignition_evidence": (
                persistent_reignition_evidence
            ),
            "persistent_wave_context": {
                "relationship": (
                    "SAME_WAVE" if same_wave
                    else "PRIOR_WAVE_SAME_SYMBOL" if historical_waves
                    else "NO_HISTORY"
                ),
                "same_wave": same_wave,
                "latest_historical_wave": (
                    historical_waves[0] if historical_waves else None
                ),
                "historical_wave_count": len(historical_waves),
                "monitoring": "MONITOR_ONLY",
                "approved_for_trade": False,
            },
            "wave_id": f"{symbol}:{anchor_time}",
            "observed_activity_status": observed_status,
            "observed_activity_priority": observed_priority,
            "classification": classification.get("status", "UNKNOWN"),
            "score": ranking.get("score"),
            "hunt_score": ranking.get("hunt_score"),
            "fast_state": fast_state.get("state", "UNKNOWN"),
            "persisted_fast_state": persisted_fast_state,
            "effective_fast_state": effective_fast_state,
            "reignition_gate": reignition_gate,
            "safety_evidence": safety_evidence,
            "integrated_observation": integrated_observation,
            "persisted_ignition_seen": persisted_ignition_seen,
            "fast_state_comparison": state_comparison,
            "ignition_seen": fast_state.get("ignition_seen", False),
            "wave_monitor": wave_monitor,
            "ranking_lane": item.get(
                "ranking_lane", "INSUFFICIENT_DATA"
            ),
            "wake_freshness": item.get(
                "wake_freshness", "UNKNOWN"
            ),
            "preliminary_priority": item.get(
                "priority", 0
            ),
            "preliminary_scores": item.get(
                "scores", {}
            ),
            "final_decision": {
                "status": final_decision.get("status", "BLOCKED"),
                "reason": final_decision.get(
                    "reason", "FINAL_CONFIRMATION_UNAVAILABLE"
                ),
            },
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
        }



        # Historical observations never enter verified FAST.
        historical_rows = [
            r for r in historical_by_wave.get(
                (symbol, anchor_time), []
            )
            if type(r.get("observation_time")) is int
            and r["observation_time"] <= scan_time
        ]

        result["historical_fast_wake_evidence"] = {
            "observations": len(historical_rows),
            "latest_observation_time": max(
                (r["observation_time"] for r in historical_rows),
                default=None,
            ),
            "evidence_status": "HISTORICAL_FAST_OBSERVED",
            "coverage_verified": False,
            "approved_for_trade": False,
            "status": "REPORT_ONLY",
        }

        # V3_UNVERIFIED_WAVE_MONITOR_V1
        # Observed FAST may seed price tracking, never trade approval.
        if (
            result.get("fast_evidence_status") == "FAST_UNVERIFIED"
            and fast_monitor_rows
        ):
            from kian_v3_upgrade_engine import KianV3UpgradeEngine
            import math

            observed_rows = [
                row for row in fast_monitor_rows
                if row.get("evidence_status") == "FAST_OBSERVED"
                and type(row.get("time")) is int
                and anchor_time <= row["time"] <= scan_time
                and type(row.get("price")) in (int, float)
                and math.isfinite(row["price"])
                and row["price"] > 0
                and row.get("coverage_verified") is False
                and row.get("real_alert_sent") is False
            ]

            if observed_rows:
                latest = max(observed_rows, key=lambda row: row["time"])
                monitor_engine = KianV3UpgradeEngine(
                    state_path=root / "tracker.json",
                    unified_state_path=root / "unified.json",
                )
                result["unverified_wave_tracking"] = {
                    "storage_status": monitor_engine.observe_price(
                        symbol,
                        anchor_time,
                        latest["time"],
                        latest["price"],
                    ),
                    "evidence_status": "FAST_UNVERIFIED",
                    "approved_for_trade": False,
                    "real_alert_sent": False,
                }

        result["fast_freshness"] = item.get("fast_freshness", "UNKNOWN")
        candidate_results.append(result)

    early_watch_resume = {
        "matched": 0,
        "updated": 0,
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }

    if verified_wave_observations is not None:
        if persistent_market_rows is not None or fetch_persistent_trades:
            raise ValueError("CONFLICTING_PERSISTENT_SOURCES")
        if not isinstance(verified_wave_observations, list):
            raise ValueError("INVALID_WAVE_OBSERVATIONS")

        for observation in verified_wave_observations:
            if not isinstance(observation, dict):
                raise ValueError("INVALID_WAVE_OBSERVATION")
            symbol = observation.get("symbol")
            anchor = observation.get("anchor_time")
            wave_id = observation.get("wave_id")
            if (
                not isinstance(symbol, str)
                or symbol not in allowed_irt_symbols
                or type(anchor) is not int
                or anchor <= 0
                or wave_id != f"{symbol}:{anchor}"
            ):
                raise ValueError("WAVE_IDENTITY_MISMATCH")

        persistent_result = resume_saved_waves_for_scan(
            scan_time=scan_time,
            fresh_observations=verified_wave_observations,
            allowed_irt_symbols=allowed_irt_symbols,
            lab_state_dir=lab_state_dir,
        )

    elif persistent_market_rows is not None:
        from kian_v3_early_watch import (
            resume_early_watch_from_market_rows,
        )
        early_watch_resume = resume_early_watch_from_market_rows(
            path=root / "early_watch.json",
            scan_time=scan_time,
            market_rows=persistent_market_rows,
            allowed_irt_symbols=allowed_irt_symbols,
        )

        if fetch_persistent_trades:
            raise ValueError("CONFLICTING_PERSISTENT_SOURCES")
        persistent_result = resume_saved_waves_from_market_rows(
            scan_time=scan_time,
            market_rows=persistent_market_rows,
            allowed_irt_symbols=allowed_irt_symbols,
            lab_state_dir=lab_state_dir,
        )
    elif fetch_persistent_trades:
        persistent_result = resume_saved_waves_from_trades(
            scan_time=scan_time,
            allowed_irt_symbols=allowed_irt_symbols,
            lab_state_dir=lab_state_dir,
            max_symbols=max_persistent_symbols,
        )
    else:
        persistent_result = {
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
            "fetch_skipped": True,
            "reason": "NETWORK_DISABLED_BY_DEFAULT",
        }

    if early_watch_resume.get("real_alert_sent") is not False:
        raise RuntimeError("UNSAFE_EARLY_WATCH_ALERT")

    if persistent_result.get("real_alert_sent") is not False:
        raise RuntimeError("UNSAFE_PERSISTENT_ALERT")

    from kian_v3_upgrade_engine import KianV3UpgradeEngine

    root.mkdir(parents=True, exist_ok=True)

    persistent_engine = KianV3UpgradeEngine(
        state_path=root / "tracker.json",
        unified_state_path=root / "unified.json",
    )
    saved_waves = persistent_engine.list_persistent_waves(
        allowed_irt_symbols
    )
    saved_wave_ids = {w["wave_id"] for w in saved_waves}
    candidate_wave_ids = {
        r["wave_id"] for r in candidate_results
    }
    persistent_only = [
        w for w in saved_waves
        if w["wave_id"] not in candidate_wave_ids
    ]

    import math

    # Independent observed FAST ranking; display only.
    # Never modifies verified hunt scores or trading decisions.
    persistent_observation_watch = [
        {
            "symbol": wave["symbol"],
            "wave_id": wave["wave_id"],
            "anchor_time": wave["anchor_time"],
            "last_time": wave["last_time"],
            "status": "PERSISTENT_OBSERVATION_ONLY",
            "fast_evidence_status": "FAST_UNVERIFIED",
            "observed_activity_priority": "WAITING",
            "approved_for_trade": False,
            "real_alert_sent": False,
        }
        for wave in persistent_only
    ]

    observed_order = {
        "HIGH_WATCH": 0,
        "NORMAL_WATCH": 1,
        "WAITING": 2,
        "INVALID": 3,
    }

    def observed_sort_key(result):
        report = result.get("candidate_report") or {}
        priority = report.get(
            "observed_activity_priority", "WAITING"
        )
        score = report.get("hunt_score")
        if (
            type(score) not in (int, float)
            or not math.isfinite(score)
        ):
            score = 0

        reignition = report.get(
            "reignition_activity_watch"
        ) or {}

        reignition_priority = (
            0
            if (
                priority == "HIGH_WATCH"
                and reignition.get("status")
                == "REIGNITION_ACTIVITY_WATCH"
            )
            else 1
        )

        return (
            observed_order.get(priority, 2),
            reignition_priority,
            -score,
            result.get("symbol", ""),
            result.get("wave_id", ""),
        )

    observed_candidate_ranking = [
        {
            "symbol": result["symbol"],
            "wave_id": result["wave_id"],
            "observed_activity_priority": (
                result.get("candidate_report", {}).get(
                    "observed_activity_priority", "WAITING"
                )
            ),
            "hunt_score": result.get(
                "candidate_report", {}
            ).get("hunt_score"),
            "reignition_activity_watch": (
                result.get("candidate_report", {})
                .get("reignition_activity_watch", {
                    "status": "NOT_READY",
                    "evidence_status": "UNVERIFIED_MONITOR_ONLY",
                    "approved_for_trade": False,
                    "real_alert_sent": False,
                })
            ),
            "wave_relationship": (
                result.get("candidate_report", {})
                .get("persistent_wave_context", {})
                .get("relationship", "UNKNOWN")
            ),
            "reignition_gate": (
                result.get("candidate_report", {})
                .get("reignition_gate")
            ),
            "fast_state": (
                result.get("candidate_report", {})
                .get("fast_state", "UNKNOWN")
            ),
            "observed_activity_status": (
                result.get("candidate_report", {})
                .get("observed_activity_status", "UNKNOWN")
            ),
            "fast_freshness": result.get(
                "fast_freshness", "UNKNOWN"
            ),
            "fast_evidence_status": result.get(
                "fast_evidence_status", "FAST_UNVERIFIED"
            ),
            "final_decision": "BLOCKED",
            "candle_reason": (
                result.get("engine_result", {})
                .get("candle_diagnostics", {})
                .get("reason", "CANDLE_DIAGNOSTICS_UNAVAILABLE")
            ),
            "final_reason": (
                result.get("candidate_report", {})
                .get("final_decision", {})
                .get("reason", "FINAL_REASON_UNAVAILABLE")
            ),
            "safety_reason": (
                result.get("candidate_report", {})
                .get("safety_evidence", {})
                .get("reason", "SAFETY_REASON_UNAVAILABLE")
            ),
            "real_alert_sent": False,
        }
        for result in sorted(
            candidate_results,
            key=observed_sort_key,
        )
    ]

    return {
        "scan_time": scan_time,
        "scan_summary": {
            "candidate_waves": len(candidate_results),
            "persistent_waves": len(saved_waves),
            "early_watch_updated": early_watch_resume["updated"],
            "early_watch_matched": early_watch_resume["matched"],
            "persistent_only_waves": len(persistent_only),
            "persistent_observations": persistent_result.get(
                "applied_count", 0
            ),
            "network_fetch_skipped": persistent_result.get(
                "fetch_skipped", False
            ),
            "status": "MONITOR_ONLY",
        },
        "candidate_count": len(candidate_results),
        "candidate_wave_ids": sorted(processed_waves),
        "candidate_results": candidate_results,
        "observed_candidate_ranking": observed_candidate_ranking,
        "persistent": persistent_result,
        "early_watch_resume": early_watch_resume,
        "saved_waves": saved_waves,
        "persistent_only_waves": persistent_only,
        "persistent_observation_watch": persistent_observation_watch,
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }
