import json, os
from pathlib import Path

DATA_FILE = Path(os.environ.get("RADAR_DATA_FILE", "tabdeal_radar_v21_data.json"))
LOG_FILE = Path("green_v21_log.json")

EXCLUDE = {
    "BTCIRT","ETHIRT","SOLIRT","XRPIRT",
    "ADAIRT","BNBIRT","TRXIRT","DOGEIRT"
}

def is_green(x):
    return (
        x.get("status") == "EARLY"
        and float(x.get("hunt_score") or 0) >= 60
        and int(x.get("persistence") or 0) >= 2
        and float(x.get("vr") or 0) >= 1
        and float(x.get("va") or 0) >= 2
        and float(x.get("buy_ratio") or 0) >= 10
        and float(x.get("p15") or 0) <= 1.5
    )

data = json.loads(DATA_FILE.read_text())

if LOG_FILE.exists():
    log = json.loads(LOG_FILE.read_text())
else:
    log = []

known = {(x["symbol"], x["time"]) for x in log}

added = 0

for symbol, history in data.items():

    if symbol in EXCLUDE or not symbol.endswith("IRT"):
        continue

    for x in history:

        if not is_green(x):
            continue

        t = x.get("time")
        price = float(x.get("price") or 0)

        if not t or price <= 0:
            continue

        key = (symbol, t)

        if key in known:
            continue

        log.append({
            "symbol": symbol,
            "time": t,
            "price": price,
            "hunt": x.get("hunt_score"),
            "persistence": x.get("persistence"),
            "vr": x.get("vr"),
            "va": x.get("va"),
            "buy": x.get("buy_ratio"),
            "bs": x.get("bs"),
            "ob": x.get("ob"),
            "p15": x.get("p15"),
            "breakout": x.get("breakout"),
            "result_1h": None,
            "result_4h": None
        })

        known.add(key)
        added += 1

log.sort(key=lambda x: x["time"])

LOG_FILE.write_text(
    json.dumps(log, ensure_ascii=False, indent=2)
)

print("NEW GREEN EVENTS =", added)
print("TOTAL LOGGED =", len(log))
print("FILE =", LOG_FILE)

# --------------------------------------------------
# Update 1H / 4H results for logged Green events
# --------------------------------------------------

history_by_symbol = data

updated = 0

for event in log:
    symbol = event["symbol"]
    t0 = event["time"]
    p0 = float(event["price"] or 0)

    if p0 <= 0 or symbol not in history_by_symbol:
        continue

    history = history_by_symbol[symbol]

    for hours, field in [(1, "result_1h"), (4, "result_4h")]:

        # نتیجه‌ای که قبلاً نهایی شده دوباره تغییر نکند
        if event.get(field) is not None:
            continue

        end_time = t0 + hours * 3600000

        # فقط وقتی بازه زمانی کامل شده نتیجه را نهایی کن
        latest_time = max(
            [x.get("time", 0) or 0 for x in history],
            default=0
        )

        if latest_time < end_time:
            continue

        prices = [
            float(x.get("price"))
            for x in history
            if x.get("time")
            and t0 < x["time"] <= end_time
            and float(x.get("price") or 0) > 0
        ]

        if not prices:
            continue

        max_up = (max(prices) / p0 - 1) * 100
        max_down = (min(prices) / p0 - 1) * 100

        event[field] = {
            "max_up": round(max_up, 2),
            "max_down": round(max_down, 2)
        }

        updated += 1

LOG_FILE.write_text(
    json.dumps(log, ensure_ascii=False, indent=2)
)

print("RESULTS UPDATED =", updated)

done1 = sum(x.get("result_1h") is not None for x in log)
done4 = sum(x.get("result_4h") is not None for x in log)

print("1H RESULTS =", done1, "/", len(log))
print("4H RESULTS =", done4, "/", len(log))
