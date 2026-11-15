
import json
import fcntl
from contextlib import contextmanager
import math
import os
import time
from pathlib import Path

import requests

BASE = "https://api1.tabdeal.org"
STATE = Path(__file__).with_name(
    "kian_v3_trade_collector_state.json"
)

def fetch_trades(symbol, limit=1000):
    if (
        not isinstance(symbol, str)
        or not symbol.isascii()
        or not symbol.isalnum()
        or not symbol.endswith("IRT")
    ):
        raise ValueError("IRT_SYMBOL_REQUIRED")

    r = requests.get(
        BASE + "/r/api/v1/trades",
        params={"symbol": symbol, "limit": limit},
        timeout=12,
    )
    r.raise_for_status()
    data = r.json()

    if not isinstance(data, list):
        raise ValueError("INVALID_RESPONSE")

    result = []

    for row in data:
        if not isinstance(row, dict):
            raise ValueError("INVALID_TRADE")

        tid = int(row["id"])
        ts = int(row["time"])
        price = float(row["price"])
        qty = float(row["qty"])

        if (
            tid <= 0 or ts <= 0
            or not math.isfinite(price)
            or not math.isfinite(qty)
            or price <= 0 or qty <= 0
        ):
            raise ValueError("INVALID_VALUES")

        result.append({
            "id": tid,
            "time": ts,
            "price": price,
            "qty": qty,
        })

    return sorted(result, key=lambda x: (x["time"], x["id"]))


@contextmanager
def _collector_state_lock(target):
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def collect_once(symbol, state_path=None):
    if state_path is None:
        raise ValueError("EXPLICIT_V3_STATE_PATH_REQUIRED")

    safe_root = (
        Path.home() / "kian_v3_upgrade_lab"
    ).resolve()
    target = Path(state_path).resolve()

    if target == safe_root or safe_root not in target.parents:
        raise ValueError("STATE_PATH_OUTSIDE_V3_LAB")

    trades = fetch_trades(symbol)

    if not trades:
        return {"status": "BLOCKED", "reason": "NO_TRADES"}

    with _collector_state_lock(target):
        if target.exists():
            state = json.loads(target.read_text())
        else:
            state = {"version": 1, "markets": {}}

        markets = state.setdefault("markets", {})
        previous = markets.get(symbol, {})

        old = previous.get("trades", [])
        old_ids = {x["id"] for x in old}
        new_ids = {x["id"] for x in trades}

        overlap = bool(old_ids & new_ids)
        first_run = not old

        merged = {x["id"]: x for x in old}

        for row in trades:
            merged[row["id"]] = row

        combined = sorted(
            merged.values(),
            key=lambda x: (x["time"], x["id"]),
        )

        # Preserve only the latest 24 hours.
        newest = combined[-1]["time"]
        combined = [
            x for x in combined
            if newest - x["time"] <= 86400000
        ]

        if first_run:
            status = "WARMING_UP"
        elif overlap:
            status = "OVERLAP_OBSERVED"
        else:
            status = "GAP_SUSPECTED"

        now_ms = int(time.time() * 1000)
        previous_fetch_ms = previous.get("last_fetch_ms")

        gap_ms = (
            now_ms - previous_fetch_ms
            if type(previous_fetch_ms) is int
            and 0 <= previous_fetch_ms <= now_ms
            else None
        )

        coverage_event = {
            "fetch_ms": now_ms,
            "status": status,
            "overlap": overlap,
            "gap_ms": gap_ms,
            "first_trade_ms": trades[0]["time"],
            "last_trade_ms": trades[-1]["time"],
            "received_count": len(trades),
        }

        history = previous.get("coverage_history", [])
        if not isinstance(history, list):
            history = []

        history = history + [coverage_event]

        markets[symbol] = {
            "trades": combined,
            "last_status": status,
            "last_fetch_ms": now_ms,
            "coverage_history": history,
            "coverage_verified": False,
        }

        temp = target.with_suffix(".json.tmp")

        with temp.open("w") as f:
            json.dump(state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp, target)

    return {
        "status": status,
        "symbol": symbol,
        "stored_trades": len(combined),
        "overlap": overlap,
        "real_alert_sent": False,
    }

if __name__ == "__main__":
    raise SystemExit(
        "Use the controlled V3 collection runner; "
        "direct collection is disabled."
    )
