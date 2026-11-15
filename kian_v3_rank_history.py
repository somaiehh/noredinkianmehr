import json
import os
import fcntl
import tempfile
from pathlib import Path

class RankHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = Path(str(path) + ".lock")

    @staticmethod
    def momentum(history):
        if len(history) < 4:
            return False
        recent = history[-4:]
        if any(
            x.get("present", True) is False
            or type(x.get("rank")) is not int
            for x in recent
        ):
            return False
        ranks = [x["rank"] for x in recent]
        improvements = sum(
            ranks[i] < ranks[i - 1]
            for i in range(1, 4)
        )
        net_gain = ranks[0] - ranks[-1]
        return improvements >= 2 and net_gain >= 3

    def update(self, scan_id, scan_time, ranked_symbols,
               real_scan=False):
        if real_scan is not True:
            raise ValueError("REAL_SCAN_REQUIRED")
        if not isinstance(scan_id, str) or not scan_id:
            raise ValueError("INVALID_SCAN_ID")
        if type(scan_time) is not int or scan_time <= 0:
            raise ValueError("INVALID_SCAN_TIME")
        if not isinstance(ranked_symbols, list):
            raise ValueError("INVALID_RANKED_LIST")
        if len(ranked_symbols) != len(set(ranked_symbols)):
            raise ValueError("DUPLICATE_SYMBOLS")
        if any(
            not isinstance(s, str) or not s.endswith("IRT")
            for s in ranked_symbols
        ):
            raise ValueError("IRT_SYMBOLS_REQUIRED")

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)

            if self.path.exists():
                state = json.loads(self.path.read_text())
            else:
                state = {
                    "last_scan_id": None,
                    "last_scan_time": 0,
                    "symbols": {},
                }

            if scan_id == state["last_scan_id"]:
                return {"status": "DUPLICATE_SCAN"}

            if scan_time <= state["last_scan_time"]:
                return {"status": "STALE_SCAN"}

            current_ranks = {
                symbol: rank
                for rank, symbol in enumerate(ranked_symbols, 1)
            }

            for symbol, item in state["symbols"].items():
                if symbol not in current_ranks:
                    item["history"].append({
                        "scan_id": scan_id,
                        "time": scan_time,
                        "rank": None,
                        "present": False,
                    })
                    item["history"] = item["history"][-5:]

            for symbol, rank in current_ranks.items():
                item = state["symbols"].setdefault(
                    symbol, {"history": []}
                )
                item["history"].append({
                    "scan_id": scan_id,
                    "time": scan_time,
                    "rank": rank,
                    "present": True,
                })
                item["history"] = item["history"][-5:]

            state["last_scan_id"] = scan_id
            state["last_scan_time"] = scan_time

            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix=".rank_v3_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

            result = {}
            for symbol in ranked_symbols:
                h = state["symbols"][symbol]["history"]
                result[symbol] = {
                    "rank": h[-1]["rank"],
                    "previous_rank": (
                        h[-2]["rank"]
                        if len(h) > 1
                        and h[-2].get("present", True)
                        else None
                    ),
                    "momentum": self.momentum(h),
                    "history": h,
                }

            return {
                "status": "SAVED",
                "scan_id": scan_id,
                "symbols": result,
                "real_alert_sent": False,
            }
