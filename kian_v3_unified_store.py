import json
import math
import os
import fcntl
import tempfile
from pathlib import Path

from kian_v3_state_machine_v1 import active, ignition

class UnifiedWaveStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = Path(str(path) + ".lock")

    def update(self, wave_id, row):
        t = row.get("time")
        p = row.get("price")
        if type(t) is not int or t <= 0:
            raise ValueError("INVALID_TIME")
        if type(p) not in (int, float) or not math.isfinite(p) or p <= 0:
            raise ValueError("INVALID_PRICE")

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.lock.open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)

            if self.path.exists():
                data = json.loads(self.path.read_text())
            else:
                data = {"waves": {}}

            waves = data["waves"]
            w = waves.get(wave_id)

            if w is None:
                w = {
                    "last_time": None,
                    "last_price": None,
                    "peak_price": p,
                    "open_correction": None,
                    "reignitions": [],
                    "fast_state": "QUIET_WATCH",
                    "ever_active": False,
                    "quiet_after_active": False,
                    "ignition_seen": False,
                    "last_fast_row": None,
                    "fast_history": [],
                }

            previous_time = w["last_time"]
            if previous_time is not None:
                if t < previous_time:
                    return {"status": "STALE_REJECTED"}
                if t == previous_time:
                    if p != w["last_price"]:
                        raise ValueError("SAME_TIME_PRICE_CONFLICT")
                    return {"status": "DUPLICATE"}

            previous_fast = w["last_fast_row"]
            now_active = active(row)
            now_ignition = ignition(previous_fast, row)

            if now_active and w["quiet_after_active"]:
                state = "REIGNITION_CANDIDATE"
                if now_ignition:
                    w["ignition_seen"] = True
            elif now_ignition:
                state = "IGNITION_CANDIDATE"
                w["ignition_seen"] = True
            elif now_active:
                state = "ACTIVITY_DETECTED"
            else:
                state = "QUIET_WATCH"

            if now_active:
                w["ever_active"] = True
                w["quiet_after_active"] = False
            elif w["ever_active"]:
                w["quiet_after_active"] = True

            w["fast_state"] = state
            w["last_fast_row"] = row

            history = w.get("fast_history")
            if not isinstance(history, list):
                history = []

            history = [
                item for item in history
                if isinstance(item, dict)
                and type(item.get("time")) is int
                and item["time"] < t
            ]

            history.append(dict(row))
            w["fast_history"] = history[-12:]

            peak = float(w["peak_price"])
            correction = w["open_correction"]
            price_event = "UPDATED"

            if correction is None:
                if p > peak:
                    w["peak_price"] = p
                elif p <= peak * 0.95:
                    w["open_correction"] = {
                        "start_time": t,
                        "prior_peak": peak,
                        "low": p,
                    }
                    price_event = "CORRECTION_OPENED"
            else:
                if p < correction["low"]:
                    correction["low"] = p
                if p > correction["prior_peak"]:
                    w["reignitions"].append({
                        "episode_time": correction["start_time"],
                        "confirmation_time": t,
                        "prior_peak": correction["prior_peak"],
                        "correction_low": correction["low"],
                        "breakout_price": p,
                        "notification_sent": False,
                    })
                    w["open_correction"] = None
                    w["peak_price"] = p
                    price_event = "REIGNITION_RECORDED"

            w["last_time"] = t
            w["last_price"] = p
            waves[wave_id] = w

            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix=".kian_v3_",
                suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

            return {
                "status": "SAVED",
                "price_event": price_event,
                "fast_state": state,
                "ignition_seen": w["ignition_seen"],
                "real_alert_sent": False,
            }
