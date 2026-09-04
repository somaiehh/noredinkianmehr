import json
from pathlib import Path

DATA_FILE = Path("tabdeal_radar_v21_data.json")
LOG_FILE = Path("early_watch_log.json")

EXCLUDE = {
    "BTCIRT","ETHIRT","SOLIRT","XRPIRT",
    "ADAIRT","BNBIRT","TRXIRT","DOGEIRT",
    "USDTIRT"
}

with DATA_FILE.open() as f:
    data = json.load(f)

if LOG_FILE.exists():
    with LOG_FILE.open() as f:
        log = json.load(f)
else:
    log = []

seen = {
    (x.get("symbol"), x.get("time"), x.get("level"))
    for x in log
}

TWO_HOURS = 2 * 60 * 60 * 1000

last_logged_time = {}
for x in log:
    sym = x.get("symbol")
    t = x.get("time")
    if sym and t:
        last_logged_time[sym] = max(last_logged_time.get(sym, 0), t)

new_events = 0

def qualifies(x):
    status = str(x.get("status") or "")
    hunt = float(x.get("hunt_score") or 0)
    p15 = float(x.get("p15") or 0)
    bs = float(x.get("bs") or 0)
    va = float(x.get("va") or 0)
    vr = float(x.get("vr") or 0)

    if status != "EARLY":
        return None

    if hunt < 40:
        return None

    if p15 > 2.0:
        return None

    if bs < 1.0:
        return None

    if not (va >= 2.0 or vr >= 1.0):
        return None

    return "STRONG" if hunt >= 50 else "WATCH"


for symbol, history in data.items():
    if not symbol.endswith("IRT"):
        continue

    if symbol in EXCLUDE:
        continue

    for x in history:
        price = float(x.get("price") or 0)
        t = x.get("time")

        if price <= 0 or not t:
            continue

        level = qualifies(x)

        if not level:
            continue

        key = (symbol, t, level)

        if key in seen:
            continue

        last_t = last_logged_time.get(symbol)

        if last_t is not None and t - last_t < TWO_HOURS:
            continue

        log.append({
            "symbol": symbol,
            "time": t,
            "price": price,
            "level": level,
            "hunt": x.get("hunt_score"),
            "persistence": x.get("persistence"),
            "p15": x.get("p15"),
            "vr": x.get("vr"),
            "va": x.get("va"),
            "buy": x.get("buy_ratio"),
            "bs": x.get("bs"),
            "breakout": x.get("breakout"),
            "result_15m": None,
            "result_1h": None,
            "result_4h": None,
            "result_12h": None,
        })

        seen.add(key)
        last_logged_time[symbol] = t
        new_events += 1

log.sort(key=lambda x: x.get("time") or 0)

with LOG_FILE.open("w") as f:
    json.dump(log, f, ensure_ascii=False, indent=2)

watch = sum(1 for x in log if x.get("level") == "WATCH")
strong = sum(1 for x in log if x.get("level") == "STRONG")

print("NEW EARLY WATCH EVENTS =", new_events)
print("TOTAL =", len(log))
print("WATCH =", watch)
print("STRONG =", strong)

# -------------------------------------------------
# UPDATE OUTCOMES: 15M / 1H / 4H / 12H
# -------------------------------------------------

def calc_result(history, t0, p0, minutes):
    end = t0 + minutes * 60 * 1000

    latest_time = max(
        (r.get("time") or 0 for r in history),
        default=0
    )

    # هنوز افق زمانی کامل نشده
    if latest_time < end:
        return None

    prices = [
        float(r.get("price") or 0)
        for r in history
        if r.get("time")
        and t0 < r["time"] <= end
        and float(r.get("price") or 0) > 0
    ]

    if not prices:
        return None

    return {
        "max_up": round((max(prices) / p0 - 1) * 100, 2),
        "max_down": round((min(prices) / p0 - 1) * 100, 2),
    }


updated = 0

for event in log:
    symbol = event.get("symbol")
    t0 = event.get("time")
    p0 = float(event.get("price") or 0)

    history = data.get(symbol, [])

    if not symbol or not t0 or p0 <= 0 or not history:
        continue

    for field, minutes in [
        ("result_15m", 15),
        ("result_1h", 60),
        ("result_4h", 240),
        ("result_12h", 720),
    ]:
        if event.get(field) is None:
            result = calc_result(history, t0, p0, minutes)

            if result is not None:
                event[field] = result
                updated += 1


with LOG_FILE.open("w") as f:
    json.dump(log, f, ensure_ascii=False, indent=2)


print("RESULTS UPDATED =", updated)

for field in [
    "result_15m",
    "result_1h",
    "result_4h",
    "result_12h",
]:
    n = sum(1 for x in log if x.get(field) is not None)
    print(field, "=", n, "/", len(log))

print("\n" + "="*80)
print("WATCH vs STRONG PERFORMANCE")
print("="*80)

for level in ["WATCH", "STRONG"]:
    print("\n", level)

    for field in [
        "result_1h",
        "result_4h",
        "result_12h",
    ]:
        rows = [
            x[field]
            for x in log
            if x.get("level") == level
            and x.get(field) is not None
        ]

        if not rows:
            print(" ", field, "N=0")
            continue

        n = len(rows)
        ups = [r["max_up"] for r in rows]
        dns = [r["max_down"] for r in rows]

        print(
            " ", field,
            "N=", n,
            "+3=", round(sum(v >= 3 for v in ups)/n*100,1),
            "+5=", round(sum(v >= 5 for v in ups)/n*100,1),
            "+10=", round(sum(v >= 10 for v in ups)/n*100,1),
            "-3=", round(sum(v <= -3 for v in dns)/n*100,1)
        )
