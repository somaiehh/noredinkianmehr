import json, os, time

STATE_FILE = "hunt_entry_v1_shadow.json"
DEDUP_MS = 12 * 60 * 60 * 1000

# Frozen prospective rule — DO NOT TUNE during validation
def is_entry_v1(x):
    try:
        return (
            float(x.get("va") or 0) >= 9.0
            and float(x.get("p15") if x.get("p15") is not None else 999) <= 2.0
            and float(x.get("vr") if x.get("vr") is not None else 999) <= 8.0
            and x.get("status") in ("EARLY", "PRE_EARLY")
            and float(x.get("hunt_score") or 0) >= 80.0
        )
    except (TypeError, ValueError):
        return False

def load_state():
    try:
        with open(STATE_FILE) as f:
            x = json.load(f)
            return x if isinstance(x, list) else []
    except:
        return []

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def register_entry_v1(candidates, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    state = load_state()
    added = []

    for x in candidates:
        if not is_entry_v1(x):
            continue

        symbol = x.get("symbol")
        if not symbol:
            continue

        # same-symbol 12h wave dedup
        recent = [
            r for r in state
            if r.get("symbol") == symbol
            and 0 <= now_ms - int(r.get("time", 0)) < DEDUP_MS
        ]
        if recent:
            continue

        try:
            price = float(x.get("price") or 0)
        except:
            price = 0

        if price <= 0:
            continue

        rec = {
            "symbol": symbol,
            "time": now_ms,
            "price": price,
            "hunt_score": x.get("hunt_score"),
            "status": x.get("status"),
            "p15": x.get("p15"),
            "vr": x.get("vr"),
            "va": x.get("va"),
            "bs": x.get("bs"),
            "breakout": x.get("breakout"),
            "persistence": x.get("persistence"),
            "rule": "ENTRY_V1_FROZEN",
            "status_24h": "WATCHING",
            "levels": [],
            "max_gain_pct": 0.0,
            "max_drawdown_pct": 0.0
        }

        state.append(rec)
        added.append(rec)

    if added:
        save_state(state)

    for r in added:
        print(
            "HUNT_V1_SHADOW:",
            r["symbol"],
            f"price={r['price']}",
            f"Hunt={r['hunt_score']}",
            f"p15={r['p15']}",
            f"VR={r['vr']}",
            f"VA={r['va']}"
        )

    return added

LEVELS = [3, 5, 10, 20]
HORIZON_MS = 24 * 60 * 60 * 1000

def update_entry_v1_outcomes(data, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    state = load_state()
    changed = False

    for rec in state:
        if rec.get("status_24h") in ("FAILED", "EXPIRED") and rec.get("mature_24h"):
            continue

        symbol = rec.get("symbol")
        anchor_time = int(rec.get("time", 0))
        anchor_price = float(rec.get("price", 0) or 0)

        if not symbol or anchor_time <= 0 or anchor_price <= 0:
            continue

        end_time = anchor_time + HORIZON_MS

        future = sorted(
            [
                r for r in data.get(symbol, [])
                if r.get("time")
                and r.get("price")
                and anchor_time < int(r["time"]) <= end_time
                and float(r["price"]) > 0
            ],
            key=lambda r: int(r["time"])
        )

        confirmed = 3 in rec.get("levels", [])

        for r in future:
            t = int(r["time"])
            p = float(r["price"])
            gain = 100.0 * (p / anchor_price - 1.0)

            if gain > float(rec.get("max_gain_pct", 0.0)):
                rec["max_gain_pct"] = round(gain, 4)
                changed = True

            if gain < float(rec.get("max_drawdown_pct", 0.0)):
                rec["max_drawdown_pct"] = round(gain, 4)
                changed = True

            # Official lifecycle:
            # -3 before first +3 = FAILED
            if not confirmed and gain <= -3.0:
                rec["status_24h"] = "FAILED"
                rec["failure_time"] = t
                rec["failure_price"] = p
                rec["failure_pct"] = round(gain, 4)
                changed = True
                break

            for level in LEVELS:
                if gain >= level and level not in rec.get("levels", []):
                    rec.setdefault("levels", []).append(level)
                    rec[f"level_{level}_time"] = t
                    rec[f"level_{level}_price"] = p
                    changed = True

                    if level == 3:
                        confirmed = True
                        rec["status_24h"] = "CONFIRMED_MOVE"
                        rec["confirmed_time"] = t
                        rec["confirmed_price"] = p

        rec["levels"] = sorted(set(rec.get("levels", [])))

        if now_ms >= end_time:
            if not rec.get("mature_24h"):
                rec["mature_24h"] = True
                changed = True

            if rec.get("status_24h") == "WATCHING":
                rec["status_24h"] = "EXPIRED"
                changed = True

    if changed:
        save_state(state)

    return state
