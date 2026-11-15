import json
import math
from pathlib import Path
from collections import defaultdict, Counter

WINDOWS = ("1m", "3m", "5m")

def valid(x):
    return type(x) in (int, float) and math.isfinite(x)

def metrics(row, window):
    v = row.get(window)
    if not isinstance(v, dict):
        return None
    t, q = v.get("trades"), v.get("volume")
    if not (valid(t) and valid(q) and t >= 0 and q >= 0):
        return None
    return t, q, v

def active(row):
    for w in WINDOWS:
        m = metrics(row, w)
        if m and m[0] > 0 and m[1] > 0:
            return True
    return False

def ignition(previous, current):
    if previous is None:
        return False
    for w in WINDOWS:
        a, b = metrics(previous, w), metrics(current, w)
        if a is None or b is None:
            continue
        if b[0] > a[0] and b[1] > a[1]:
            move = b[2].get("price_move")
            if valid(move) and move > 0:
                return True
    return False

class WaveMachine:
    def __init__(self):
        self.state = "QUIET_WATCH"
        self.last_time = None
        self.last_row = None
        self.ever_active = False
        self.quiet_after_active = False
        self.transitions = []
        self.ignition_seen = False

    def update(self, row):
        t = row.get("time")
        if type(t) is not int:
            return False
        if self.last_time is not None and t <= self.last_time:
            return False

        now_active = active(row)
        now_ignition = ignition(self.last_row, row)

        if now_ignition:
            new_state = "IGNITION_CANDIDATE"
            self.ignition_seen = True
        elif now_active and self.quiet_after_active:
            new_state = "REIGNITION_CANDIDATE"
        elif now_active:
            new_state = "ACTIVITY_DETECTED"
        else:
            new_state = "QUIET_WATCH"

        if new_state != self.state:
            self.transitions.append(
                (t, self.state, new_state)
            )

        self.state = new_state

        if now_active:
            self.ever_active = True
            self.quiet_after_active = False
        elif self.ever_active:
            self.quiet_after_active = True

        self.last_time = t
        self.last_row = row
        return True

def main():
    path = Path.home() / "tabdeal_cloud/fast_watch_shadow.json"
    records = json.loads(path.read_text())["records"]

    groups = defaultdict(list)
    for r in records:
        if not isinstance(r, dict):
            continue
        symbol = r.get("symbol")
        anchor = r.get("anchor_time")
        t = r.get("time")
        if isinstance(symbol, str) and type(anchor) is int and type(t) is int:
            if t >= anchor:
                groups[(symbol, anchor)].append(r)

    counts = Counter()
    examples = []

    for key, rows in groups.items():
        machine = WaveMachine()
        for row in sorted(rows, key=lambda r: r["time"]):
            machine.update(row)

        counts["WAVES"] += 1
        counts["TRANSITIONS"] += len(machine.transitions)
        counts["FINAL_" + machine.state] += 1

        if machine.ignition_seen:
            counts["EVER_IGNITION_CANDIDATE"] += 1

        if any(
            to == "REIGNITION_CANDIDATE"
            for _, _, to in machine.transitions
        ):
            counts["EVER_REIGNITION_CANDIDATE"] += 1

        if key[0] in ("VENOMIRT", "XAIIRT", "RAYIRT"):
            examples.append((
                key[0],
                machine.state,
                machine.transitions[:8]
            ))

    print("=== KIAN V3 STATE MACHINE V1 ===")
    for name, value in sorted(counts.items()):
        print(name, value)
    print("SELECTED_EXAMPLES:", examples)
    print("NOTE: REIGNITION MEANS ACTIVITY RETURN, NOT PRICE PUMP")
    print("NO_BUY_SIGNALS")
    print("NO_PRODUCTION_CHANGES")

if __name__ == "__main__":
    main()
