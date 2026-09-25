import json, os

STATE_FILE = "hunt_quiet_wake_state.json"
WINDOW_MS = 24 * 60 * 60 * 1000

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def quiet_features(history, anchor_time):
    start = anchor_time - WINDOW_MS
    pre = [r for r in history if r.get("time") and start <= int(r["time"]) < anchor_time]
    zero = 0
    valid = 0
    for r in pre:
        try:
            p = float(r.get("price") or 0)
        except Exception:
            p = 0
        if p > 0:
            valid += 1
        else:
            zero += 1
    return {"zero": zero, "valid": valid, "rows": len(pre)}

def is_quiet_wake_v1(features):
    return features["zero"] >= 10 and features["valid"] <= 3

def register_quiet_wake(symbol, history, anchor_time, anchor_price, wake_type):
    state = load_state()
    key = symbol + ":" + str(int(anchor_time))
    if key in state:
        return None

    f = quiet_features(history, int(anchor_time))
    q2 = is_quiet_wake_v1(f)

    state[key] = {
        "symbol": symbol,
        "anchor_time": int(anchor_time),
        "anchor_price": float(anchor_price),
        "wake_type": wake_type,
        "quiet_q2": q2,
        "zero_pre24h": f["zero"],
        "valid_pre24h": f["valid"],
        "rows_pre24h": f["rows"],
        "status": "WATCHING",
        "levels": [],
        "max_gain_pct": 0.0,
        "max_drawdown_pct": 0.0
    }
    save_state(state)
    return q2

def update_quiet_wake(data, now_ms):
    found = 0
    for symbol, history in data.items():
        if not history:
            continue
        for row in history:
            if not (row.get("wake_short") or row.get("wake_deep")):
                continue
            t = int(row.get("time") or 0)
            p = float(row.get("price") or 0)
            if t <= 0 or p <= 0:
                continue
            if now_ms - t > 30 * 60 * 1000:
                continue
            wt = "WAKE_DEEP" if row.get("wake_deep") else "WAKE_SHORT"
            result = register_quiet_wake(symbol, history, t, p, wt)
            if result is not None:
                found += 1
                group = "Q2" if result else "CONTROL"
                print("HUNT_LEDGER:", group, symbol, wt, "anchor", p)
    return found

LEVELS = [3, 5, 10, 15, 20]
HORIZON_MS = 24 * 60 * 60 * 1000

def update_quiet_wake_outcomes(data, now_ms):
    state = load_state()
    changed = False

    for key, wave in state.items():
        symbol = wave.get("symbol")
        anchor_time = int(wave.get("anchor_time") or 0)
        anchor_price = float(wave.get("anchor_price") or 0)

        if anchor_time <= 0 or anchor_price <= 0:
            continue

        history = data.get(symbol, [])
        future = [
            r for r in history
            if r.get("time")
            and anchor_time < int(r["time"]) <= anchor_time + HORIZON_MS
            and float(r.get("price") or 0) > 0
        ]
        future.sort(key=lambda r: int(r["time"]))

        hit3 = 3 in wave.get("levels", [])
        levels = list(wave.get("levels", []))

        for r in future:
            t = int(r["time"])
            p = float(r["price"])
            gain = 100.0 * (p / anchor_price - 1.0)

            if gain > float(wave.get("max_gain_pct") or 0):
                wave["max_gain_pct"] = round(gain, 4)
                changed = True

            if gain < float(wave.get("max_drawdown_pct") or 0):
                wave["max_drawdown_pct"] = round(gain, 4)
                changed = True

            if not hit3 and gain <= -3.0:
                if wave.get("status") != "FAILED":
                    wave["status"] = "FAILED"
                    wave["failure_time"] = t
                    wave["failure_price"] = p
                    wave["failure_pct"] = round(gain, 4)
                    changed = True
                break

            for level in LEVELS:
                if gain >= level and level not in levels:
                    levels.append(level)
                    wave["level_%s_time" % level] = t
                    wave["level_%s_price" % level] = p
                    changed = True
                    if level == 3:
                        hit3 = True
                        wave["status"] = "CONFIRMED_MOVE"
                        wave["confirmed_time"] = t
                        wave["confirmed_price"] = p

        wave["levels"] = sorted(set(levels))

        if now_ms >= anchor_time + HORIZON_MS:
            wave["mature_24h"] = True
            if wave.get("status") == "WATCHING":
                wave["status"] = "EXPIRED"
            changed = True

    if changed:
        save_state(state)

    return state

SNAPSHOT_WINDOWS = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
}

def update_quiet_wake_snapshots(data, now_ms):
    state = load_state()
    changed = False

    for wave in state.values():
        symbol = wave.get("symbol")
        anchor_time = int(wave.get("anchor_time") or 0)
        anchor_price = float(wave.get("anchor_price") or 0)

        if anchor_time <= 0 or anchor_price <= 0:
            continue

        history = data.get(symbol, [])
        snapshots = wave.setdefault("snapshots", {})

        for name, delay in SNAPSHOT_WINDOWS.items():
            if name in snapshots:
                continue

            target = anchor_time + delay
            if now_ms < target:
                continue

            rows = [
                r for r in history
                if r.get("time")
                and int(r["time"]) >= target
                and float(r.get("price") or 0) > 0
            ]

            if not rows:
                continue

            r = min(rows, key=lambda x: int(x["time"]))
            t = int(r["time"])
            p = float(r["price"])
            gain = 100.0 * (p / anchor_price - 1.0)

            snapshots[name] = {
                "time": t,
                "price": p,
                "gain_pct": round(gain, 4),
                "delay_min": round((t - anchor_time) / 60000.0, 1),
                "status_at_save": wave.get("status")
            }
            changed = True

    if changed:
        save_state(state)

    return state
