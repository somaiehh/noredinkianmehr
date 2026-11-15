import json
import math
import os
import fcntl
import tempfile
from pathlib import Path

DRAWDOWN_TRIGGER = 5.0

class IncrementalTracker:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = Path(str(path) + ".lock")

    def update_batch(self, observations):
        import copy

        if not isinstance(observations, list):
            raise ValueError("INVALID_BATCH")

        normalized = []
        for item in observations:
            if not isinstance(item, dict):
                raise ValueError("INVALID_BATCH_ITEM")

            wave_id = item.get("wave_id")
            timestamp = item.get("time")
            price = item.get("price")

            if not isinstance(wave_id, str) or not wave_id:
                raise ValueError("INVALID_WAVE_ID")
            if type(timestamp) is not int or timestamp <= 0:
                raise ValueError("INVALID_TIME")
            if (
                type(price) not in (int, float)
                or not math.isfinite(price)
                or price <= 0
            ):
                raise ValueError("INVALID_PRICE")

            normalized.append(
                (wave_id, timestamp, float(price))
            )

        seen = {}
        for wave_id, timestamp, price in normalized:
            key = (wave_id, timestamp)
            if key in seen and seen[key] != price:
                raise ValueError("SAME_TIME_PRICE_CONFLICT")
            seen[key] = price

        normalized = sorted(
            (
                (wave_id, timestamp, price)
                for (wave_id, timestamp), price in seen.items()
            ),
            key=lambda item: (item[1], item[0]),
        )

        with open(self.lock, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    original = json.loads(self.path.read_text())
                else:
                    original = {"version": 4, "waves": {}}

                if (
                    not isinstance(original, dict)
                    or original.get("version") != 4
                    or not isinstance(original.get("waves"), dict)
                ):
                    raise ValueError("INVALID_STATE")

                working = copy.deepcopy(original)
                outcomes = []

                for wave_id, timestamp, price in normalized:
                    wave = working["waves"].get(wave_id)

                    if wave is None:
                        raise ValueError("UNKNOWN_PERSISTENT_WAVE")

                    if timestamp < wave["last_time"]:
                        outcomes.append("STALE_REJECTED")
                        continue

                    if timestamp == wave["last_time"]:
                        if price != wave["last_price"]:
                            raise ValueError("SAME_TIME_PRICE_CONFLICT")
                        outcomes.append("DUPLICATE")
                        continue

                    wave["last_time"] = timestamp
                    wave["last_price"] = price

                    correction = wave["open_correction"]
                    outcome = "UPDATED"

                    if correction is None:
                        if price > wave["peak_price"]:
                            wave["peak_price"] = price
                            wave["peak_time"] = timestamp

                        drawdown = 100 * (
                            price / wave["peak_price"] - 1
                        )

                        if drawdown <= -DRAWDOWN_TRIGGER:
                            wave["open_correction"] = {
                                "start_time": timestamp,
                                "prior_peak": wave["peak_price"],
                                "low": price,
                                "low_time": timestamp,
                            }
                            outcome = "CORRECTION_OPENED"
                    else:
                        if price < correction["low"]:
                            correction["low"] = price
                            correction["low_time"] = timestamp

                        if price > correction["prior_peak"]:
                            wave["reignitions"].append({
                                "episode_time": correction["start_time"],
                                "confirmation_time": timestamp,
                                "prior_peak": correction["prior_peak"],
                                "correction_low": correction["low"],
                                "breakout_price": price,
                                "notification_sent": False,
                            })
                            wave["open_correction"] = None
                            wave["peak_price"] = price
                            wave["peak_time"] = timestamp
                            outcome = "REIGNITION_RECORDED"

                    outcomes.append(outcome)

                if working != original:
                    fd, tmp = tempfile.mkstemp(
                        dir=self.path.parent,
                        prefix=".incremental_batch_",
                    )
                    try:
                        with os.fdopen(fd, "w") as f:
                            json.dump(working, f)
                            f.flush()
                            os.fsync(f.fileno())

                        os.replace(tmp, self.path)

                        dfd = os.open(
                            str(self.path.parent), os.O_RDONLY
                        )
                        try:
                            os.fsync(dfd)
                        finally:
                            os.close(dfd)
                    finally:
                        Path(tmp).unlink(missing_ok=True)

                return outcomes
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def update(self, wave_id, timestamp, price):
        if not isinstance(wave_id, str) or not wave_id:
            raise ValueError("INVALID_WAVE_ID")
        if type(timestamp) is not int or timestamp <= 0:
            raise ValueError("INVALID_TIME")
        if (
            type(price) not in (int, float)
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("INVALID_PRICE")

        price = float(price)

        with open(self.lock, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    data = json.loads(self.path.read_text())
                else:
                    data = {"version": 4, "waves": {}}

                if (
                    not isinstance(data, dict)
                    or data.get("version") != 4
                    or not isinstance(data.get("waves"), dict)
                ):
                    raise ValueError("INVALID_STATE")

                wave = data["waves"].get(wave_id)

                if wave is None:
                    wave = {
                        "anchor_time": timestamp,
                        "anchor_price": price,
                        "last_time": timestamp,
                        "last_price": price,
                        "peak_price": price,
                        "peak_time": timestamp,
                        "open_correction": None,
                        "reignitions": []
                    }
                    data["waves"][wave_id] = wave
                    result = "NEW_WAVE"

                else:
                    last_time = wave["last_time"]

                    if timestamp < last_time:
                        return "STALE_REJECTED"

                    if timestamp == last_time:
                        if price == wave["last_price"]:
                            return "DUPLICATE"
                        raise ValueError("SAME_TIME_PRICE_CONFLICT")

                    wave["last_time"] = timestamp
                    wave["last_price"] = price

                    correction = wave["open_correction"]
                    result = "UPDATED"

                    if correction is None:
                        if price > wave["peak_price"]:
                            wave["peak_price"] = price
                            wave["peak_time"] = timestamp

                        drawdown = 100 * (
                            price / wave["peak_price"] - 1
                        )

                        if drawdown <= -DRAWDOWN_TRIGGER:
                            wave["open_correction"] = {
                                "start_time": timestamp,
                                "prior_peak": wave["peak_price"],
                                "low": price,
                                "low_time": timestamp
                            }
                            result = "CORRECTION_OPENED"

                    else:
                        if price < correction["low"]:
                            correction["low"] = price
                            correction["low_time"] = timestamp

                        if price > correction["prior_peak"]:
                            wave["reignitions"].append({
                                "episode_time": correction["start_time"],
                                "confirmation_time": timestamp,
                                "prior_peak": correction["prior_peak"],
                                "correction_low": correction["low"],
                                "breakout_price": price,
                                "notification_sent": False
                            })
                            wave["open_correction"] = None
                            wave["peak_price"] = price
                            wave["peak_time"] = timestamp
                            result = "REIGNITION_RECORDED"

                fd, tmp = tempfile.mkstemp(
                    dir=self.path.parent,
                    prefix=".incremental_"
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(data, f)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.path)
                    dfd = os.open(
                        str(self.path.parent), os.O_RDONLY
                    )
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                finally:
                    Path(tmp).unlink(missing_ok=True)

                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    print("=== KIAN V3 INCREMENTAL TRACKER V4 ===")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "tracker.json"
        tracker = IncrementalTracker(path)

        # Synthetic multi-day wave with a correction and recovery.
        observations = [
            (1000, 100.0),
            (2000, 120.0),
            (3000, 110.0),
            (4000, 105.0),
            (86405000, 115.0),
            (172806000, 121.0)
        ]

        results = [
            tracker.update("TESTIRT:1000", t, p)
            for t, p in observations
        ]

        restarted = IncrementalTracker(path)
        duplicate = restarted.update(
            "TESTIRT:1000", 172806000, 121.0
        )
        stale = restarted.update(
            "TESTIRT:1000", 3000, 110.0
        )

        state = json.loads(path.read_text())
        wave = state["waves"]["TESTIRT:1000"]

        assert results == [
            "NEW_WAVE",
            "UPDATED",
            "CORRECTION_OPENED",
            "UPDATED",
            "UPDATED",
            "REIGNITION_RECORDED"
        ]
        assert duplicate == "DUPLICATE"
        assert stale == "STALE_REJECTED"
        assert len(wave["reignitions"]) == 1
        assert wave["open_correction"] is None
        assert wave["peak_price"] == 121.0

        print("EVENT_RESULTS:", results)
        print("RESTART_DUPLICATE:", duplicate)
        print("STALE_EVENT:", stale)
        print("REIGNITIONS:", len(wave["reignitions"]))
        print("FINAL_PEAK:", wave["peak_price"])
        print("MULTI_DAY_PERSISTENCE: PASS")
        print("REPLAY_DEDUP: PASS")
        print("STALE_PROTECTION: PASS")
        print("NOTIFICATIONS_SENT: 0")
        print("TEMP_DATA_REMOVED: YES")
        print("NO_PRODUCTION_CHANGES")
