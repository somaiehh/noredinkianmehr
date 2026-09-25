import json
import os
import tempfile
import time

STATE_FILE = "move_early_v2_shadow.json"

WINDOW_MS = 120 * 60 * 1000
HORIZON_MS = 24 * 60 * 60 * 1000

UP_LEVELS = [3, 5, 10, 20]
FAIL_LEVEL = -3


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(state):
    folder = os.path.dirname(os.path.abspath(STATE_FILE))

    fd, tmp = tempfile.mkstemp(
        prefix=".move_early_v2_",
        suffix=".tmp",
        dir=folder
    )

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(tmp, STATE_FILE)

    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def register_breakouts(waves, history, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    state = load_state()
    added = 0

    for wave in waves:
        symbol = wave.get("symbol")
        anchor_t = int(wave.get("anchor_time") or 0)
        anchor_price = num(wave.get("anchor_price"))

        if not symbol or not anchor_t or anchor_price <= 0:
            continue

        rows = sorted(
            history.get(symbol, []),
            key=lambda r: int(r.get("time") or 0)
        )

        signal = next(
            (
                r for r in rows
                if anchor_t < int(r.get("time") or 0)
                <= anchor_t + WINDOW_MS
                and bool(r.get("breakout"))
                and num(r.get("price")) > 0
            ),
            None
        )

        if signal is None:
            continue

        signal_t = int(signal.get("time") or 0)
        signal_price = num(signal.get("price"))

        try:
            with open("move_early_v2_start_ms.txt") as f:
                start_ms = int(f.read().strip())
        except (OSError, ValueError):
            start_ms = now_ms

        if signal_t < start_ms:
            continue

        key = f"{symbol}:{anchor_t}"

        if key in state:
            continue

        pre_gain = 100 * (
            signal_price / anchor_price - 1
        )

        state[key] = {
            "symbol": symbol,
            "anchor_time": anchor_t,
            "anchor_price": anchor_price,
            "anchor_kind": wave.get("anchor_kind"),
            "signal_time": signal_t,
            "signal_price": signal_price,
            "delay_min": round(
                (signal_t - anchor_t) / 60000, 2
            ),
            "pre_gain_pct": round(pre_gain, 4),

            "status_at_signal": signal.get("status"),
            "hunt_score": signal.get("hunt_score"),
            "p15": signal.get("p15"),
            "va": signal.get("va"),
            "vr": signal.get("vr"),
            "bs": signal.get("bs"),
            "persistence": signal.get("persistence"),

            "result": "WATCHING",
            "confirmed": False,
            "failed_first": False,
            "levels": [],
            "max_gain_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "mature_24h": False,
            "created_at": now_ms
        }

        added += 1

        print(
            "MOVE_EARLY_V2_SHADOW",
            symbol,
            f"PRE={pre_gain:+.2f}%",
            f"DELAY={(signal_t-anchor_t)/60000:.0f}m"
        )

    if added:
        save_state(state)

    return added

def update_outcomes(history, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    state = load_state()
    changed = False

    for key, rec in state.items():
        if rec.get("mature_24h"):
            continue

        symbol = rec.get("symbol")
        signal_t = int(rec.get("signal_time") or 0)
        signal_price = num(rec.get("signal_price"))

        if not symbol or not signal_t or signal_price <= 0:
            continue

        end24 = signal_t + HORIZON_MS

        rows = sorted(
            (
                r for r in history.get(symbol, [])
                if signal_t < int(r.get("time") or 0) <= end24
                and num(r.get("price")) > 0
            ),
            key=lambda r: int(r.get("time") or 0)
        )

        first_up3 = None
        first_dn3 = None
        max_gain = 0.0
        max_dd = 0.0
        levels = set(rec.get("levels") or [])
        for row in rows:
            t = int(row.get("time") or 0)
            price = num(row.get("price"))

            gain = 100 * (price / signal_price - 1)

            max_gain = max(max_gain, gain)
            max_dd = min(max_dd, gain)

            if first_up3 is None and gain >= 3:
                first_up3 = t

            if first_dn3 is None and gain <= -3:
                first_dn3 = t

            for level in UP_LEVELS:
                if gain >= level:
                    levels.add(level)

        rec["max_gain_pct"] = round(max_gain, 4)
        rec["max_drawdown_pct"] = round(max_dd, 4)
        rec["levels"] = sorted(levels)

        if first_dn3 is not None and (
            first_up3 is None or first_dn3 < first_up3
        ):
            rec["result"] = "FAIL_FIRST"
            rec["failed_first"] = True

        elif first_up3 is not None and (
            first_dn3 is None or first_up3 < first_dn3
        ):
            rec["result"] = "CLEAN"
            rec["confirmed"] = True

        if rows:
            last_t = int(rows[-1].get("time") or 0)

            if last_t >= end24 - 30 * 60 * 1000:
                rec["mature_24h"] = True
                rec["matured_at"] = now_ms

                if rec["result"] == "WATCHING":
                    rec["result"] = "NO_MOVE"

        rec["updated_at"] = now_ms
        changed = True

    if changed:
        save_state(state)

    return state
def summary():
    state = load_state()
    rows = list(state.values())

    mature = [x for x in rows if x.get("mature_24h")]
    clean = [x for x in mature if x.get("result") == "CLEAN"]
    failed = [x for x in mature if x.get("result") == "FAIL_FIRST"]
    no_move = [x for x in mature if x.get("result") == "NO_MOVE"]

    print(
        "MOVE_EARLY_V2_SUMMARY",
        f"TOTAL={len(rows)}",
        f"WATCHING={len(rows) - len(mature)}",
        f"MATURE={len(mature)}",
        f"CLEAN={len(clean)}",
        f"FAIL_FIRST={len(failed)}",
        f"NO_MOVE={len(no_move)}"
    )



def run_shadow():
    try:
        with open("move_tracker_state.json") as f:
            tracker_state = json.load(f)

        with open("move_tracker_archive.json") as f:
            tracker_archive = json.load(f)

        with open("tabdeal_radar_v21_data.json") as f:
            history = json.load(f)

        waves = list(tracker_state.values()) + list(tracker_archive)

        added = register_breakouts(waves, history)
        update_outcomes(history)

        print(
            "MOVE_EARLY_V2_RUN",
            f"NEW={added}"
        )

        summary()

    except Exception as e:
        print(
            "MOVE_EARLY_V2_ERROR:",
            type(e).__name__,
            str(e)
        )


