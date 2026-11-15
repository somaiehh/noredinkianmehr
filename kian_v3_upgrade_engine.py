from kian_v3_wake_core import update_strong_wake_ledger, update_pre_wake_ledger
from kian_v3_sequence_v1 import sequence_score_v1
from pathlib import Path
import json
from kian_v3_incremental_tracker_v4 import IncrementalTracker
from kian_v3_state_machine_v1 import WaveMachine
from kian_v3_unified_store import UnifiedWaveStore

ROOT = Path(__file__).resolve().parent

class KianV3UpgradeEngine:
    def __init__(self, state_path, unified_state_path=None):
        self.tracker = IncrementalTracker(state_path)
        self.unified_state_path = (
            Path(unified_state_path)
            if unified_state_path is not None
            else ROOT / "kian_v3_unified_state.json"
        )
        self.fast_machines = {}
        self.production_enabled = False
        self.real_alerts_enabled = False

    def list_persistent_waves(self, allowed_irt_symbols):
        import fcntl

        if not isinstance(allowed_irt_symbols, (set, frozenset)):
            raise ValueError("INVALID_MARKET_UNIVERSE")

        allowed = {
            s for s in allowed_irt_symbols
            if isinstance(s, str)
            and s.endswith("IRT")
            and s not in {
                "BTCIRT", "XRPIRT", "ADAIRT",
                "SOLIRT", "USDTIRT"
            }
        }

        path = self.tracker.path
        with open(self.tracker.lock, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                if not path.exists():
                    return []

                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(data, dict)
                    or data.get("version") != 4
                    or not isinstance(data.get("waves"), dict)
                ):
                    raise ValueError("INVALID_TRACKER_STATE")

                result = []
                for wave_id, wave in data["waves"].items():
                    if not isinstance(wave_id, str):
                        continue
                    symbol, sep, anchor = wave_id.rpartition(":")
                    if (
                        not sep
                        or symbol not in allowed
                        or not anchor.isdigit()
                        or not isinstance(wave, dict)
                    ):
                        continue

                    anchor_time = int(anchor)
                    last_time = wave.get("last_time")
                    if (
                        anchor_time <= 0
                        or type(last_time) is not int
                        or last_time < anchor_time
                    ):
                        continue

                    result.append({
                        "symbol": symbol,
                        "anchor_time": anchor_time,
                        "wave_id": wave_id,
                        "last_time": last_time,
                        "needs_fresh_observation": True,
                        "status": "MONITOR_ONLY",
                    })

                return sorted(
                    result,
                    key=lambda x: (x["last_time"], x["wave_id"])
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def resume_persistent_waves(
        self, allowed_irt_symbols, fresh_observations
    ):
        import math

        if not isinstance(fresh_observations, list):
            raise ValueError("INVALID_FRESH_OBSERVATIONS")

        pending = self.list_persistent_waves(
            allowed_irt_symbols
        )
        indexed = {
            item["wave_id"]: item for item in pending
        }

        results = []
        seen = {}

        for row in fresh_observations:
            if not isinstance(row, dict):
                continue

            symbol = row.get("symbol")
            anchor_time = row.get("anchor_time")
            timestamp = row.get("time")
            price = row.get("price")

            if (
                not isinstance(symbol, str)
                or type(anchor_time) is not int
                or type(timestamp) is not int
                or type(price) not in (int, float)
                or not math.isfinite(price)
                or price <= 0
            ):
                continue

            wave_id = f"{symbol}:{anchor_time}"
            previous = indexed.get(wave_id)

            if previous is None:
                continue

            if timestamp <= previous["last_time"]:
                continue

            key = (wave_id, timestamp)
            if key in seen:
                if seen[key] != float(price):
                    raise ValueError("SAME_TIME_PRICE_CONFLICT")
                continue
            seen[key] = float(price)

            results.append({
                "symbol": symbol,
                "anchor_time": anchor_time,
                "wave_id": wave_id,
                "time": timestamp,
                "price": float(price),
                "status": "FRESH_OBSERVATION_READY",
                "real_alert_sent": False,
            })

        ordered = sorted(
            results,
            key=lambda x: (x["time"], x["wave_id"])
        )

        conflicts = {}
        for item in ordered:
            key = (item["wave_id"], item["time"])
            conflicts.setdefault(key, set()).add(item["price"])

        if any(len(prices) > 1 for prices in conflicts.values()):
            raise ValueError("SAME_TIME_PRICE_CONFLICT")

        batch = [
            {
                "wave_id": item["wave_id"],
                "time": item["time"],
                "price": item["price"],
            }
            for item in ordered
        ]

        outcomes = self.tracker.update_batch(batch)

        if len(outcomes) != len(ordered):
            raise RuntimeError("BATCH_RESULT_COUNT_MISMATCH")

        applied = [
            {
                **item,
                "tracker_result": outcome,
            }
            for item, outcome in zip(ordered, outcomes)
        ]

        return {
            "pending_waves": len(pending),
            "fresh_matches": len(ordered),
            "applied_count": len(applied),
            "observations": applied,
            "status": "MONITOR_ONLY",
            "real_alert_sent": False,
        }

    def observe_price(self, symbol, anchor_time, timestamp, price):
        if not isinstance(symbol, str) or not symbol.endswith("IRT"):
            raise ValueError("IRT_SYMBOL_REQUIRED")
        if type(anchor_time) is not int or anchor_time <= 0:
            raise ValueError("INVALID_ANCHOR_TIME")
        if type(timestamp) is not int or timestamp < anchor_time:
            raise ValueError("INVALID_EVENT_TIME")

        wave_id = f"{symbol}:{anchor_time}"
        return self.tracker.update(wave_id, timestamp, price)

    def update_rank_history(
        self, scan_id, scan_time, ranked_symbols,
        rank_state_path=None, real_scan=False
    ):
        from kian_v3_rank_history import RankHistory

        path = (
            Path(rank_state_path)
            if rank_state_path is not None
            else ROOT / "kian_v3_rank_history_state.json"
        )

        tracker = RankHistory(path)
        return tracker.update(
            scan_id=scan_id,
            scan_time=scan_time,
            ranked_symbols=ranked_symbols,
            real_scan=real_scan,
        )

    def observe_fast(self, symbol, anchor_time, row):
        result = self.process_fast_persistent(
            symbol, anchor_time, row
        )
        return {
            "wave_id": f"{symbol}:{anchor_time}",
            "accepted": result.get("status") == "SAVED",
            "state": result.get("fast_state"),
            "ignition_seen": result.get("ignition_seen", False),
            "storage_status": result.get("status"),
            "real_alert_sent": False,
        }


    def process_fast_observation(self, symbol, anchor_time, row):
        return self.process_fast_persistent(
            symbol, anchor_time, row
        )


    def process_fast_persistent(self, symbol, anchor_time, row):
        if not isinstance(symbol, str) or not symbol.endswith("IRT"):
            raise ValueError("IRT_SYMBOL_REQUIRED")
        if type(anchor_time) is not int or anchor_time <= 0:
            raise ValueError("INVALID_ANCHOR_TIME")
        if not isinstance(row, dict):
            raise ValueError("FAST_ROW_REQUIRED")
        if type(row.get("time")) is not int or row["time"] < anchor_time:
            raise ValueError("INVALID_FAST_TIME")

        wave_id = f"{symbol}:{anchor_time}"
        unified_path = self.unified_state_path
        if unified_path.resolve() == self.tracker.path.resolve():
            raise RuntimeError("STATE_PATH_COLLISION")
        return UnifiedWaveStore(
            unified_path
        ).update(wave_id, row)

    def describe(self):
        return {
            "project": "Tabdeal Kian Radar V3 Upgrade",
            "mode": "ISOLATED_BUILD",
            "production_enabled": False,
            "real_alerts_enabled": False,
            "integrated": ["MULTI_DAY_TRACKER", "IGNITION_STATE_MACHINE"],
            "pending": [
                "SOURCE_ADAPTER",
                "TEMPORAL_ADAPTER",
                "FEATURE_ADAPTER",
                "SAFE_STORE",
                "RADAR_CORE",
                "ALERT_ROUTING",
            ],
        }


    def evaluate_sequence(self, history, current):
        from kian_v3_sequence_v1 import sequence_score_v1
        return sequence_score_v1(history, current)

    def evaluate_strong_wake(self, data, now_ms):
        from kian_v3_wake_core import update_strong_wake_ledger
        return update_strong_wake_ledger(data, now_ms)

    def evaluate_pre_wake(self, data, now_ms):
        from kian_v3_wake_core import update_pre_wake_ledger
        return update_pre_wake_ledger(data, now_ms)


    def process_candidate(
        self,
        symbol,
        anchor_time,
        fast_row,
        history=None,
        current=None,
        wake_data=None,
        now_ms=None,
        evidence=None,
        confirmation=None,
        score_row=None,
        fast_already_saved=False,
    ):
        result = {
            "symbol": symbol,
            "anchor_time": anchor_time,
            "fast": None,
            "sequence": None,
            "strong_wake": None,
            "pre_wake": None,
            "real_alert_sent": False,
            "final_decision": {
                "status": "BLOCKED",
                "reason": "CONFIRMATION_NOT_PROVIDED",
                "real_alert_sent": False,
            },
            "classification": {
                "status": "WATCH",
                "stage": "PRELIMINARY",
                "reason": "NO_EVIDENCE",
            },
        }

        if fast_already_saved:
            result["fast"] = {
                "status": "ALREADY_SAVED",
                "real_alert_sent": False,
            }
        else:
            result["fast"] = self.process_fast_persistent(
                symbol, anchor_time, fast_row
            )

        if history is not None and current is not None:
            result["sequence"] = self.evaluate_sequence(
                history, current
            )

        if wake_data is not None and now_ms is not None:
            result["strong_wake"] = self.evaluate_strong_wake(
                wake_data, now_ms
            )
            result["pre_wake"] = self.evaluate_pre_wake(
                wake_data, now_ms
            )

        if evidence is not None:
            result["classification"] = self.classify_candidate(evidence)
            result["classification"]["stage"] = "PRELIMINARY"

        if confirmation is not None:
            result["final_decision"] = self.confirm_candidate(
                result["classification"], confirmation
            )

        if score_row is not None:
            score_result = self.evaluate_score_output(score_row)
            result["score_analysis"] = score_result
            result["classification"] = score_result["classification"]

            if confirmation is None:
                result["final_decision"] = score_result["final_decision"]
            else:
                result["final_decision"] = self.confirm_candidate(
                    result["classification"], confirmation
                )

        return result


    def classify_candidate(self, evidence):
        import math
        keys = ("p15", "va", "ta", "bs", "book", "vr", "breakout")

        if not isinstance(evidence, dict):
            return {"status": "REJECT", "reason": "INVALID_EVIDENCE",
                    "real_alert_sent": False}

        if any(evidence.get(k) is None for k in keys):
            return {"status": "WATCH", "reason": "MISSING_EVIDENCE",
                    "real_alert_sent": False}

        try:
            p15, va, ta, bs, book, vr = (
                float(evidence[k]) for k in keys[:-1]
            )
            if not all(map(math.isfinite, (p15, va, ta, bs, book, vr))):
                raise ValueError("NONFINITE")
            if type(evidence["breakout"]) is not bool:
                raise ValueError("INVALID_BREAKOUT")
            bo = evidence["breakout"]
        except (ValueError, TypeError, OverflowError):
            return {"status": "REJECT", "reason": "INVALID_VALUES",
                    "real_alert_sent": False}

        activity_signal = (
            va >= 1.2 or ta >= 1.2 or bs >= 1.2 or book >= 0.65
        )
        strong_accumulation = (
            -0.8 <= p15 <= 0.5
            and (va >= 1.8 or ta >= 1.8)
            and (bs >= 1.3 or book >= 0.65)
        )

        if p15 <= 0 and activity_signal and not strong_accumulation:
            status = "WATCH_ACCUMULATION"
        else:
            activity_confirmed = va >= 1.5 or ta >= 1.5
            power_breakout = (
                bo and vr >= 3.0 and va >= 3.0 and bs >= 1.5
            )
            demand_confirmed = (
                (bs >= 1.5 and book >= 0.35)
                or book >= 0.75
                or power_breakout
            )
            early = (
                0.05 <= p15 <= 3.0
                and activity_confirmed
                and demand_confirmed
            )
            pre_early = (
                not early
                and -0.8 <= p15 <= 0.5
                and activity_confirmed
                and (strong_accumulation or bs >= 1.25 or book >= 0.65)
            )

            status = (
                "EARLY" if early else
                "PRE_EARLY" if pre_early else
                "WATCH"
            )

        return {
            "status": status,
            "strong_accumulation": strong_accumulation,
            "source_rules": "RADAR_ORIGINAL",
            "real_alert_sent": False,
        }


    def confirm_candidate(self, classification, confirmation,
                          candle_data=None, cutoff_ms=None):
        allowed = ("PRE_EARLY", "EARLY")

        if not isinstance(classification, dict):
            return {
                "status": "BLOCKED",
                "reason": "INVALID_CLASSIFICATION",
                "real_alert_sent": False,
            }

        preliminary = classification.get("status")

        if preliminary not in allowed:
            return {
                "status": "WATCH",
                "reason": "NOT_EARLY_CANDIDATE",
                "real_alert_sent": False,
            }

        if not isinstance(confirmation, dict):
            return {
                "status": "BLOCKED",
                "reason": "MISSING_CONFIRMATION",
                "real_alert_sent": False,
            }

        required = (
            "trend_1h_confirmed",
            "structure_4h_intact",
            "liquidity_confirmed",
            "late_pump",
        )

        if any(type(confirmation.get(k)) is not bool for k in required):
            return {
                "status": "BLOCKED",
                "reason": "INVALID_OR_MISSING_CONFIRMATION",
                "real_alert_sent": False,
            }

        if confirmation["late_pump"]:
            return {
                "status": "BLOCKED",
                "reason": "LATE_PUMP",
                "real_alert_sent": False,
            }

        if not all(confirmation[k] for k in required[:-1]):
            return {
                "status": "WATCH",
                "reason": "CONFIRMATION_INCOMPLETE",
                "real_alert_sent": False,
            }

        if not isinstance(candle_data, dict) or type(cutoff_ms) is not int:
            return {
                "status": "BLOCKED",
                "reason": "CANDLE_DATA_NOT_PROVIDED",
                "real_alert_sent": False,
            }

        frames = (
            ("15m", 15 * 60 * 1000, 9),
            ("1h", 60 * 60 * 1000, 4),
            ("4h", 4 * 60 * 60 * 1000, 4),
        )

        quality = {}
        for name, interval, minimum in frames:
            quality[name] = self.validate_candle_quality(
                candle_data.get(name),
                interval,
                cutoff_ms,
                min_candles=minimum,
            )

        if not all(x["valid"] for x in quality.values()):
            return {
                "status": "BLOCKED",
                "reason": "MULTI_TIMEFRAME_DATA_NOT_READY",
                "candle_quality": quality,
                "real_alert_sent": False,
            }

        return {
            "status": preliminary,
            "stage": "CONFIRMED_IN_BUILD",
            "candle_quality": quality,
            "real_alert_sent": False,
        }



    def build_ignition_alert_preview(self, context):
        """Offline preview only. Never sends notifications."""
        if not isinstance(context, dict):
            raise ValueError("INVALID_EARLY_PUMP_CONTEXT")

        stage = context.get("stage", "WATCH")
        symbol = context.get("symbol", "UNKNOWN")

        if stage == "REIGNITION_REVIEW":
            title = "REIGNITION WATCH"
        elif stage == "EARLY_CANDIDATE_UNVERIFIED":
            title = "EARLY IGNITION WATCH"
        else:
            title = "KIAN V3 WATCH"

        lines = [
            f"{title} | {symbol}",
            f"Stage: {stage}",
            f"Candidate: {context.get('candidate_status')}",
            f"Hunt Score: {context.get('hunt_score')}",
            f"Rank: {context.get('rank')}",
            f"Gain: {context.get('gain_from_anchor_pct')}%",
            f"Peak Gain: {context.get('peak_gain_pct')}%",
            f"Drawdown: {context.get('drawdown_from_peak_pct')}%",
            f"Liquidity: {context.get('liquidity')}",
            f"Risk: {context.get('risk_flags')}",
            "UNVERIFIED | MONITOR ONLY | NO TRADE",
        ]

        return {
            "title": title,
            "message": "\n".join(lines),
            "priority": 4,
            "preview_only": True,
            "approved_for_trade": False,
            "real_alert_sent": False,
        }

    def evaluate_early_pump_context(
        self, hunt_context, market_safety
    ):
        if not isinstance(hunt_context, dict):
            raise ValueError("INVALID_HUNT_CONTEXT")

        safety = (
            market_safety
            if isinstance(market_safety, dict)
            else {}
        )

        classification = hunt_context.get("classification") or {}
        candidate = classification.get("status")

        flags = safety.get("risk_flags")
        if not isinstance(flags, list):
            flags = []

        if "EXTENDED_MOVE_REVIEW" in flags:
            stage = "EXTENDED_MOVE_REVIEW"
        elif "POSSIBLE_REIGNITION_REVIEW" in flags:
            stage = "REIGNITION_REVIEW"
        elif candidate in ("PRE_EARLY", "EARLY"):
            stage = "EARLY_CANDIDATE_UNVERIFIED"
        else:
            stage = "WATCH"

        preliminary_priority = hunt_context.get(
            "early_priority", "NORMAL_WATCH"
        )

        if not isinstance(market_safety, dict):
            early_priority = "SAFETY_REVIEW_WATCH"
        elif "POSSIBLE_REIGNITION_REVIEW" in flags:
            early_priority = "REIGNITION_WATCH"
        elif "EXTENDED_MOVE_REVIEW" in flags:
            early_priority = "EXTENDED_MOVE_WATCH"
        elif safety.get("status") != "APPROVED":
            early_priority = "SAFETY_REVIEW_WATCH"
        elif preliminary_priority == "HIGH_WATCH":
            early_priority = "HIGH_WATCH"
        else:
            early_priority = "NORMAL_WATCH"

        return {
            "symbol": hunt_context.get("symbol"),
            "early_priority": early_priority,
            "preliminary_priority": preliminary_priority,
            "stage": stage,
            "candidate_status": candidate,
            "ignition": hunt_context.get("ignition"),
            "rank": hunt_context.get("rank"),
            "hunt_score": hunt_context.get("hunt_score"),
            "gain_from_anchor_pct": safety.get("gain_from_anchor_pct"),
            "peak_gain_pct": safety.get("peak_gain_pct"),
            "drawdown_from_peak_pct": safety.get(
                "drawdown_from_peak_pct"
            ),
            "liquidity": safety.get("liquidity", "UNKNOWN"),
            "late_pump": safety.get("late_pump", "UNKNOWN"),
            "risk_flags": flags,
            "final_decision": {
                "status": "BLOCKED",
                "reason": "FULL_SAFETY_NOT_VERIFIED",
                "real_alert_sent": False,
            },
            "real_alert_sent": False,
        }

    def evaluate_hunt_context(
        self, score_row, fast_result=None, rank_result=None,
        anchor_time=None, scan_id=None
    ):
        score_analysis = self.evaluate_score_output(score_row)

        symbol = (
            score_row.get("symbol")
            if isinstance(score_row, dict)
            else None
        )

        ignition_state = None
        ignition_seen = None
        fast_identity_valid = False

        if isinstance(fast_result, dict):
            fast_symbol = fast_result.get("symbol")
            fast_wave_id = fast_result.get("wave_id")
            fast_identity_valid = (
                isinstance(symbol, str)
                and fast_symbol == symbol
                and isinstance(fast_wave_id, str)
                and type(anchor_time) is int
                and anchor_time > 0
                and fast_wave_id == f"{symbol}:{anchor_time}"
            )

            if fast_identity_valid:
                ignition_state = (
                    fast_result.get("fast_state")
                    or fast_result.get("state")
                )
                if type(fast_result.get("ignition_seen")) is bool:
                    ignition_seen = fast_result["ignition_seen"]

        rank = None
        previous_rank = None
        momentum = None

        if (
            isinstance(rank_result, dict)
            and rank_result.get("status") == "SAVED"
            and isinstance(scan_id, str)
            and bool(scan_id)
            and rank_result.get("scan_id") == scan_id
            and isinstance(rank_result.get("symbols"), dict)
        ):
            item = rank_result["symbols"].get(symbol)
            if isinstance(item, dict):
                rank = item.get("rank")
                previous_rank = item.get("previous_rank")
                if type(item.get("momentum")) is bool:
                    momentum = item["momentum"]

        classification_status = (
            score_analysis["classification"].get("status")
        )

        if classification_status in ("PRE_EARLY", "EARLY"):
            early_priority = "HIGH_WATCH"
        elif classification_status == "WATCH_ACCUMULATION":
            early_priority = "ACCUMULATION_WATCH"
        else:
            early_priority = "NORMAL_WATCH"

        return {
            "symbol": symbol,
            "early_priority": early_priority,
            "hunt_score": score_analysis.get("score"),
            "pre_score": score_analysis.get("pre_score"),
            "classification": score_analysis["classification"],
            "ignition": {
                "state": ignition_state,
                "seen": ignition_seen,
            },
            "rank": {
                "current": rank,
                "previous": previous_rank,
                "momentum": momentum,
            },
            "final_decision": {
                "status": "BLOCKED",
                "reason": "CANDLE_LIQUIDITY_SAFETY_NOT_VERIFIED",
                "real_alert_sent": False,
            },
            "real_alert_sent": False,
        }

    def evaluate_score_output(self, score_row):
        if not isinstance(score_row, dict):
            return {
                "classification": {
                    "status": "REJECT",
                    "reason": "INVALID_SCORE_ROW",
                },
                "final_decision": {
                    "status": "BLOCKED",
                    "reason": "INVALID_SCORE_ROW",
                },
                "real_alert_sent": False,
            }

        evidence = {
            "p15": score_row.get("p15"),
            "va": score_row.get("va"),
            "ta": score_row.get("ta"),
            "bs": score_row.get("bs"),
            "book": score_row.get("book"),
            "vr": score_row.get("vr"),
            "breakout": score_row.get("breakout"),
        }

        classification = self.classify_candidate(evidence)

        source_status = score_row.get("status")

        required_features = ("p15", "va", "ta", "bs", "book", "vr")
        import math

        missing_features = [
            k for k in required_features
            if not (
                isinstance(score_row.get(k), (int, float))
                and not isinstance(score_row.get(k), bool)
                and math.isfinite(score_row[k])
            )
        ]
        if type(score_row.get("breakout")) is not bool:
            missing_features.append("breakout")
        features_complete = not missing_features

        if not features_complete:
            classification = {
                "status": "INSUFFICIENT_DATA",
                "reason": "MISSING_OR_INVALID_SCORE_FEATURES",
                "missing_features": missing_features,
                "real_alert_sent": False,
            }
        if not features_complete:
            pass
        elif source_status == "WATCH_ACCUMULATION":
            classification = {
                "status": "WATCH_ACCUMULATION",
                "reason": "WAITING_FOR_PRICE_CONFIRMATION",
                "real_alert_sent": False,
            }
        elif source_status not in (
            "EARLY", "PRE_EARLY", "SCANNED"
        ):
            classification = {
                "status": "WATCH",
                "reason": "SOURCE_NOT_ELIGIBLE",
                "real_alert_sent": False,
            }

        classification["stage"] = "PRELIMINARY"

        return {
            "symbol": score_row.get("symbol"),
            "classification": classification,
            "source_status": score_row.get("status"),
            "pre_score": score_row.get("pre_score"),
            "score": score_row.get("score"),
            "structure1h": score_row.get("structure1h"),
            "structure4h": score_row.get("structure4h"),
            "compression15": score_row.get("compression15"),
            "final_decision": {
                "status": "BLOCKED",
                "reason": "MULTI_TIMEFRAME_NOT_YET_VERIFIED",
                "real_alert_sent": False,
            },
            "real_alert_sent": False,
        }

    def observe_wave(self, symbol, anchor_time, row):
        if not isinstance(row, dict):
            raise ValueError("WAVE_ROW_REQUIRED")

        timestamp = row.get("time")
        price = row.get("price")

        if type(timestamp) is not int:
            raise ValueError("INVALID_WAVE_TIME")
        if type(price) not in (int, float):
            raise ValueError("INVALID_WAVE_PRICE")

        fast_result = self.observe_fast(
            symbol, anchor_time, row
        )

        price_result = self.observe_price(
            symbol, anchor_time, timestamp, price
        )

        return {
            "wave_id": f"{symbol}:{anchor_time}",
            "fast": fast_result,
            "price_tracking": price_result,
            "real_alert_sent": False,
        }


    def evaluate_unified_decision(self, classification, candle_result, market_safety=None):
        candidate = (
            classification.get("status")
            if isinstance(classification, dict)
            else None
        )

        result = {
            "status": "BLOCKED",
            "candidate_status": candidate or "UNKNOWN",
            "monitoring": "WATCH",
            "real_alert_sent": False,
        }

        if candidate not in ("PRE_EARLY", "EARLY"):
            result["reason"] = "NOT_EARLY_CANDIDATE"
            return result

        result["monitoring"] = "KEEP_EARLY_CANDIDATE"

        if not isinstance(candle_result, dict):
            result["reason"] = "CANDLE_RESULT_MISSING"
            return result

        if candle_result.get("status") != "EVALUATED":
            result["reason"] = "MULTI_TIMEFRAME_DATA_NOT_READY"
            return result

        required = (
            "breakout_15m_confirmed",
            "trend_1h_confirmed",
            "structure_4h_intact",
        )

        if any(type(candle_result.get(k)) is not bool for k in required):
            result["reason"] = "INVALID_CANDLE_CONFIRMATION"
            return result

        if not all(candle_result[k] for k in required):
            result["reason"] = "MULTI_TIMEFRAME_NOT_CONFIRMED"
            return result

        result["stage"] = "CANDLE_CONFIRMED_AWAITING_SAFETY"

        if not isinstance(market_safety, dict):
            result["reason"] = "MARKET_SAFETY_MISSING"
            return result

        if market_safety.get("status") != "APPROVED":
            result["reason"] = "MARKET_SAFETY_NOT_APPROVED"
            result["market_safety_reason"] = market_safety.get(
                "reason"
            )
            return result

        result["reason"] = "FINAL_TRADE_APPROVAL_NOT_IMPLEMENTED"
        return result

    def process_unified_wave(
        self, symbol, anchor_time, row, score_row=None,
        history=None, current=None, wake_data=None, now_ms=None,
        candle_data=None, cutoff_ms=None, fetch_depth=False
    ):
        if score_row is not None:
            if not isinstance(score_row, dict):
                raise ValueError("INVALID_SCORE_ROW")
            if score_row.get("symbol") != symbol:
                raise ValueError("SYMBOL_MISMATCH")

        observation = self.observe_wave(
            symbol, anchor_time, row
        )

        components = self.process_candidate(
            symbol=symbol,
            anchor_time=anchor_time,
            fast_row=row,
            history=history,
            current=current,
            wake_data=wake_data,
            now_ms=now_ms,
            score_row=score_row,
            fast_already_saved=True,
        )

        candle_confirmation = (
            self.derive_candle_confirmation(candle_data, cutoff_ms)
            if candle_data is not None
            else {
                "status": "BLOCKED",
                "reason": "CANDLE_DATA_NOT_PROVIDED",
            }
        )

        market_input = {
            **row,
            **(self.read_wave_snapshot(symbol, anchor_time) or {}),
        }

        if (
            fetch_depth
            and components["classification"].get("status")
            in ("PRE_EARLY", "EARLY")
        ):
            market_input.update(self.safe_fetch_depth(symbol))

        market_safety = self.evaluate_market_safety(
            market_input, cutoff_ms
        )

        return {
            "symbol": symbol,
            "anchor_time": anchor_time,
            "observation": observation,
            "candle_confirmation": candle_confirmation,
            "preliminary": components.get("score_analysis") or {
                "classification": {
                    **components["classification"],
                    "score_status": "INSUFFICIENT_DATA",
                    "score_reason": "SCORE_ROW_MISSING",
                },
            },
            "components": components,
            "market_safety": market_safety,
            "final_decision": self.evaluate_unified_decision(
                components["classification"],
                candle_confirmation,
                market_safety=market_safety,
            ),
            "real_alert_sent": False,
        }


    def validate_candle_quality(self, candles, interval_ms, cutoff_ms,
                                min_candles=4):
        import math

        if (
            type(interval_ms) is not int or interval_ms <= 0
            or type(cutoff_ms) is not int or cutoff_ms <= 0
            or type(min_candles) is not int or min_candles < 1
        ):
            return {"valid": False, "reason": "INVALID_PARAMETERS"}

        if not isinstance(candles, (list, tuple)):
            return {"valid": False, "reason": "INVALID_CANDLES"}

        completed = []
        for candle in candles:
            try:
                start = candle["time"] if isinstance(candle, dict) else candle.time
                high = candle["high"] if isinstance(candle, dict) else candle.high
                low = candle["low"] if isinstance(candle, dict) else candle.low
                close = candle["close"] if isinstance(candle, dict) else candle.close

                if type(start) is not int:
                    raise ValueError("TIME")
                if start % interval_ms != 0:
                    raise ValueError("ALIGNMENT")

                values = [float(high), float(low), float(close)]
                if not all(math.isfinite(x) and x > 0 for x in values):
                    raise ValueError("PRICE")
                if not values[1] <= values[2] <= values[0]:
                    raise ValueError("OHLC")

                if start + interval_ms <= cutoff_ms:
                    completed.append(start)
            except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
                return {"valid": False, "reason": "INVALID_CANDLE"}

        if len(completed) < min_candles:
            return {
                "valid": False,
                "reason": "INSUFFICIENT_CLOSED_CANDLES",
                "closed_count": len(completed),
            }

        completed.sort()
        recent = completed[-min_candles:]

        if len(set(completed)) != len(completed):
            return {"valid": False, "reason": "DUPLICATE_CANDLES"}

        if any(
            b - a != interval_ms
            for a, b in zip(recent, recent[1:])
        ):
            return {"valid": False, "reason": "CANDLE_GAPS"}

        if recent[-1] + interval_ms != (
            cutoff_ms // interval_ms
        ) * interval_ms:
            return {"valid": False, "reason": "STALE_CANDLES"}

        return {
            "valid": True,
            "reason": "CLOSED_CONTIGUOUS_CANDLES",
            "closed_count": len(completed),
        }


    def derive_candle_confirmation(self, candle_data, cutoff_ms):
        if type(cutoff_ms) is not int or cutoff_ms <= 0:
            return {
                "status": "BLOCKED",
                "reason": "INVALID_CUTOFF",
                "real_alert_sent": False,
            }

        frames = (
            ("15m", 900000, 9),
            ("1h", 3600000, 4),
            ("4h", 14400000, 4),
        )

        if not isinstance(candle_data, dict):
            return {"status": "BLOCKED", "reason": "INVALID_CANDLE_DATA"}

        quality = {}
        closed = {}

        for name, interval, minimum in frames:
            rows = candle_data.get(name)
            quality[name] = self.validate_candle_quality(
                rows, interval, cutoff_ms, minimum
            )
            if not quality[name]["valid"]:
                return {
                    "status": "BLOCKED",
                    "reason": "INSUFFICIENT_TIMEFRAME_DATA",
                    "candle_quality": quality,
                }

            closed[name] = sorted(
                (
                    r for r in rows
                    if (
                        (r["time"] if isinstance(r, dict) else r.time)
                        + interval <= cutoff_ms
                    )
                ),
                key=lambda r: r["time"] if isinstance(r, dict) else r.time
            )

        def field(row, key):
            return row[key] if isinstance(row, dict) else getattr(row, key)

        c15 = closed["15m"]
        c1 = closed["1h"]
        c4 = closed["4h"]

        breakout15 = (
            field(c15[-1], "close")
            > max(field(x, "high") for x in c15[-9:-1])
        )

        trend1h = (
            field(c1[-1], "close") > field(c1[-2], "close")
            and field(c1[-1], "low") >= field(c1[-2], "low")
        )

        structure4h = (
            field(c4[-1], "close") >= field(c4[-2], "close")
            and field(c4[-1], "low") >= field(c4[-2], "low")
        )

        return {
            "status": "EVALUATED",
            "breakout_15m_confirmed": bool(breakout15),
            "trend_1h_confirmed": bool(trend1h),
            "structure_4h_intact": bool(structure4h),
            "candle_quality": quality,
            "real_alert_sent": False,
        }


    def evaluate_market_safety(self, market_data, cutoff_ms):
        import math

        result = {
            "status": "BLOCKED",
            "liquidity": "UNKNOWN",
            "late_pump": "UNKNOWN",
            "risk_flags": [],
            "real_alert_sent": False,
        }

        if not isinstance(market_data, dict):
            result["reason"] = "MARKET_DATA_MISSING"
            return result

        if type(cutoff_ms) is not int or cutoff_ms <= 0:
            result["reason"] = "INVALID_CUTOFF"
            return result

        def positive(value):
            if type(value) not in (int, float):
                raise ValueError("INVALID_NUMBER")
            value = float(value)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("INVALID_NUMBER")
            return value

        try:
            depth = market_data["depth"]
            timestamp = market_data["depth_time_ms"]

            if not isinstance(depth, dict) or type(timestamp) is not int:
                raise ValueError("INVALID_DEPTH")

            if not 0 <= cutoff_ms - timestamp <= 120000:
                raise ValueError("STALE_DEPTH")

            bids = depth["bids"][:10]
            asks = depth["asks"][:10]

            if not bids or not asks:
                raise ValueError("EMPTY_BOOK")

            bid_values = [
                (positive(p), positive(q)) for p, q in bids
            ]
            ask_values = [
                (positive(p), positive(q)) for p, q in asks
            ]

            bid = max(p for p, q in bid_values)
            ask = min(p for p, q in ask_values)

            if ask <= bid:
                raise ValueError("CROSSED_BOOK")

            result["spread_pct"] = round(
                200 * (ask - bid) / (ask + bid), 4
            )
            result["bid_notional_top10"] = round(
                sum(p * q for p, q in bid_values), 2
            )
            result["ask_notional_top10"] = round(
                sum(p * q for p, q in ask_values), 2
            )
            result["liquidity"] = "MEASURED_NOT_APPROVED"

        except (KeyError, TypeError, ValueError, IndexError) as e:
            result["liquidity"] = "INSUFFICIENT_DATA"
            result["risk_flags"].append("LIQUIDITY_" + str(e))

        try:
            anchor = positive(market_data["anchor_price"])
            peak = positive(market_data["peak_price"])
            last = positive(market_data["last_price"])
            timestamp = market_data["price_time_ms"]

            if type(timestamp) is not int:
                raise ValueError("INVALID_PRICE_TIME")

            if not 0 <= cutoff_ms - timestamp <= 900000:
                raise ValueError("STALE_PRICE")

            if peak < max(anchor, last):
                raise ValueError("INCONSISTENT_PEAK")

            gain = 100 * (last / anchor - 1)
            peak_gain = 100 * (peak / anchor - 1)
            drawdown = 100 * (last / peak - 1)

            result["gain_from_anchor_pct"] = round(gain, 3)
            result["peak_gain_pct"] = round(peak_gain, 3)
            result["drawdown_from_peak_pct"] = round(drawdown, 3)
            result["late_pump"] = "REVIEW_REQUIRED"

            if gain >= 15:
                result["risk_flags"].append("EXTENDED_MOVE_REVIEW")

            if peak_gain >= 30 and drawdown <= -10:
                result["risk_flags"].append("POSSIBLE_REIGNITION_REVIEW")

        except (KeyError, TypeError, ValueError, OverflowError) as e:
            result["late_pump"] = "INSUFFICIENT_DATA"
            result["risk_flags"].append("WAVE_" + str(e))

        result["reason"] = "SAFETY_THRESHOLDS_NOT_VALIDATED"
        return result


    def read_wave_snapshot(self, symbol, anchor_time):
        import json
        import fcntl
        import math

        if not isinstance(symbol, str) or not symbol.endswith("IRT"):
            raise ValueError("IRT_SYMBOL_REQUIRED")

        if type(anchor_time) is not int or anchor_time <= 0:
            raise ValueError("INVALID_ANCHOR_TIME")

        wave_id = f"{symbol}:{anchor_time}"
        path = self.tracker.path
        lock_path = self.tracker.lock

        with open(lock_path, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                if not path.exists():
                    return None

                data = json.loads(path.read_text(encoding="utf-8"))

                if (
                    not isinstance(data, dict)
                    or data.get("version") != 4
                    or not isinstance(data.get("waves"), dict)
                ):
                    raise ValueError("INVALID_TRACKER_STATE")

                wave = data["waves"].get(wave_id)

                if wave is None:
                    return None

                if not isinstance(wave, dict):
                    raise ValueError("INVALID_WAVE_RECORD")

                required = (
                    "anchor_price",
                    "peak_price",
                    "last_price",
                    "last_time",
                )

                if any(k not in wave for k in required):
                    raise ValueError("INCOMPLETE_WAVE_RECORD")

                for key in required[:3]:
                    value = wave[key]
                    if (
                        type(value) not in (int, float)
                        or not math.isfinite(value)
                        or value <= 0
                    ):
                        raise ValueError("INVALID_WAVE_PRICE")

                if (
                    type(wave["last_time"]) is not int
                    or wave["last_time"] < anchor_time
                ):
                    raise ValueError("INVALID_LAST_TIME")

                if wave["peak_price"] < max(
                    wave["anchor_price"],
                    wave["last_price"],
                ):
                    raise ValueError("INCONSISTENT_PEAK")

                return {
                    "anchor_price": wave["anchor_price"],
                    "peak_price": wave["peak_price"],
                    "last_price": wave["last_price"],
                    "price_time_ms": wave["last_time"],
                    "open_correction": wave.get("open_correction"),
                    "reignition_count": len(
                        wave.get("reignitions") or []
                    ),
                }
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


    def fetch_validated_depth(self, symbol):
        import requests
        import time
        import math

        if (
            not isinstance(symbol, str)
            or not symbol.endswith("IRT")
            or not symbol.isascii()
            or not symbol.isalnum()
        ):
            raise ValueError("IRT_SYMBOL_REQUIRED")

        # Timestamp taken BEFORE the request, conservatively.
        timestamp = int(time.time() * 1000)

        try:
            response = requests.get(
                "https://api1.tabdeal.org/r/api/v1/depth",
                params={"symbol": symbol, "limit": 10},
                timeout=12,
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("INVALID_DEPTH_RESPONSE")

            def validate_side(name):
                rows = data.get(name)

                if not isinstance(rows, list) or not rows:
                    raise ValueError("MISSING_" + name.upper())

                if len(rows) > 10:
                    raise ValueError("UNEXPECTED_DEPTH_LIMIT")

                validated = []

                for row in rows:
                    if (
                        not isinstance(row, (list, tuple))
                        or len(row) != 2
                    ):
                        raise ValueError("INVALID_ORDER_LEVEL")

                    price, quantity = row

                    if (
                        type(price) not in (str, int, float)
                        or type(quantity) not in (str, int, float)
                    ):
                        raise ValueError("INVALID_ORDER_VALUE")

                    price = float(price)
                    quantity = float(quantity)

                    if not (
                        math.isfinite(price)
                        and math.isfinite(quantity)
                        and price > 0
                        and quantity > 0
                    ):
                        raise ValueError("INVALID_ORDER_NUMBER")

                    validated.append([price, quantity])

                return validated

            bids = validate_side("bids")
            asks = validate_side("asks")

            # Ensure prices are ordered and do not cross.
            if any(
                bids[i][0] <= bids[i + 1][0]
                for i in range(len(bids) - 1)
            ):
                raise ValueError("INVALID_BID_ORDER")

            if any(
                asks[i][0] >= asks[i + 1][0]
                for i in range(len(asks) - 1)
            ):
                raise ValueError("INVALID_ASK_ORDER")

            if bids[0][0] >= asks[0][0]:
                raise ValueError("CROSSED_BOOK")

            if int(time.time() * 1000) - timestamp > 120000:
                raise ValueError("DEPTH_TOO_OLD")

            return {
                "status": "VALIDATED_SNAPSHOT",
                "symbol": symbol,
                "depth_time_ms": timestamp,
                "depth": {
                    "bids": bids,
                    "asks": asks,
                },
                "real_alert_sent": False,
            }

        except (
            requests.RequestException,
            ValueError,
            TypeError,
            OverflowError,
        ) as e:
            return {
                "status": "BLOCKED",
                "reason": type(e).__name__,
                "depth": None,
                "real_alert_sent": False,
            }


    def safe_fetch_depth(self, symbol):
        try:
            result = self.fetch_validated_depth(symbol)
            if not isinstance(result, dict):
                return {
                    "status": "BLOCKED",
                    "reason": "INVALID_FETCH_RESULT",
                }
            if result.get("status") != "VALIDATED_SNAPSHOT":
                return {
                    "status": "BLOCKED",
                    "reason": "DEPTH_UNAVAILABLE",
                }
            return result
        except Exception as e:
            return {
                "status": "BLOCKED",
                "reason": "DEPTH_FETCH_EXCEPTION",
                "error_type": type(e).__name__,
            }








if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        engine = KianV3UpgradeEngine(
            Path(folder) / "upgrade_state.json"
        )
        events = [
            (1000, 100.0),
            (2000, 120.0),
            (3000, 110.0),
            (4000, 105.0),
            (86405000, 115.0),
            (172806000, 121.0),
        ]
        results = [
            engine.observe_price(
                "VENOMIRT", 1000, timestamp, price
            )
            for timestamp, price in events
        ]
        print("=== KIAN V3 UPGRADE ENGINE ===")
        print("RESULTS:", results)
        print(json.dumps(
            engine.describe(), indent=2
        ))
        print("NO_PRODUCTION_CHANGES")
        print("REAL_ALERTS_DISABLED")

    def evaluate_sequence(self, history, current):
        return sequence_score_v1(history, current)

    def evaluate_wake(self, data, now_ms):
        strong = update_strong_wake_ledger(data, now_ms)
        pre = update_pre_wake_ledger(data, now_ms)
        return {
            "strong_wake": strong,
            "pre_wake": pre,
            "real_alert_sent": False,
        }
