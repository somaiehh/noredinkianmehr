# Tabdeal Pre-Pump Radar v2
# Public market data only; NO order placement.

import argparse, time, statistics, json, os
from dataclasses import dataclass
import requests

BASE = "https://api1.tabdeal.org"
TIMEOUT = 15

NTFY_TOPIC = "tabdeal-radar-kian-8264"

def send_ntfy(message, title="Tabdeal Radar"):
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "rotating_light"
            },
            timeout=10
        )
        r.raise_for_status()
        print("NTFY: sent")
    except requests.RequestException as e:
        print("NTFY error:", e)


def api_get(path, params=None):
    """
    Resilient public API request.

    Retry temporary Tabdeal/network failures instead of
    immediately killing or corrupting a radar scan.
    """
    last_error = None

    for attempt in range(1, 4):
        try:
            r = requests.get(
                BASE + path,
                params=params,
                timeout=TIMEOUT
            )
            r.raise_for_status()
            return r.json()

        except (requests.RequestException, ValueError) as e:
            last_error = e

            if attempt >= 3:
                raise

            # Short backoff: 1s, then 2s
            time.sleep(attempt)

    raise last_error

def mean(xs):
    return statistics.mean(xs) if xs else 0.0

def pct(a, b):
    return (a/b-1)*100 if b else 0.0

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def get_markets():
    data = api_get("/r/api/v1/exchangeInfo")
    if isinstance(data, dict):
        data = data.get("symbols", [])
    return [m for m in data if m.get("status") == "TRADING"
            and "SPOT" in m.get("permissions", ["SPOT"])
            and str(m.get("symbol", "")).endswith("IRT")]

def get_trades(symbol, limit=1000):
    return api_get("/r/api/v1/trades", {"symbol": symbol, "limit": limit})

def get_depth(symbol, limit=50):
    return api_get("/r/api/v1/depth", {"symbol": symbol, "limit": limit})

@dataclass
class Candle:
    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float

def candles(trades, interval_ms):
    buckets = {}
    for t in trades:
        k = (int(t["time"]) // interval_ms) * interval_ms
        p, q = float(t["price"]), float(t["qty"])
        v = float(t.get("quoteQty", p*q))
        if k not in buckets:
            buckets[k] = Candle(k,p,p,p,p,0)
        c = buckets[k]
        c.high = max(c.high,p); c.low = min(c.low,p)
        c.close = p; c.volume += v
    return [buckets[k] for k in sorted(buckets)]

def structure(cs):
    if len(cs) < 4: return .5
    return clamp(.5 + pct(cs[-1].close, cs[-4].close)/20)

def compression(cs):
    if len(cs) < 6: return 0
    r = [(c.high-c.low)/c.close for c in cs if c.close]
    return clamp(1-mean(r[-3:])/mean(r[:-3])) if mean(r[:-3]) else 0

def breakout(cs, lookback=8):
    if len(cs) < lookback+1: return 0
    return float(cs[-1].close > max(c.high for c in cs[-lookback-1:-1]))

def volume_profile(cs):
    """
    Robust 15m volume metrics.
    Avoids gigantic ratios when historical candles are incomplete.
    """
    if len(cs) < 3:
        return {
            "vr": 1.0,
            "va": 1.0,
            "volume": cs[-1].volume if cs else 0.0,
        }

    current = cs[-1].volume

    # فقط کندل‌های قبلی که واقعاً volume دارند
    hist = [c.volume for c in cs[:-1] if c.volume > 0]

    if not hist:
        return {
            "vr": 1.0,
            "va": 1.0,
            "volume": current,
        }

    baseline = mean(hist[-min(6, len(hist)):])

    # نسبت حجم فعلی به baseline
    vr = current / baseline if baseline > 0 else 1.0

    previous = cs[-2].volume
    va = current / previous if previous > 0 else 1.0

    # سقف منطقی برای جلوگیری از انفجار عدد
    vr = min(vr, 20.0)
    va = min(va, 10.0)

    return {
        "vr": vr,
        "va": va,
        "volume": current,
    }


def activity_profile(ts, cs):
    """
    Short-term activity profile.

    The latest 15m candle is usually incomplete.
    Normalize its volume/trade count to a 15m equivalent
    before comparing it with the previous completed candle.
    """

    if len(cs) < 2:
        return {
            "va": None,
            "ta": None,
            "valid": False,
        }

    now = int(time.time() * 1000)

    current = cs[-1]
    previous = cs[-2]

    elapsed_ms = max(60 * 1000, now - current.start)
    elapsed_ms = min(elapsed_ms, 15 * 60 * 1000)

    # Project current partial candle to a full 15m equivalent.
    time_factor = (15 * 60 * 1000) / elapsed_ms

    projected_volume = current.volume * time_factor

    if previous.volume <= 0:
        return {
            "va": None,
            "ta": None,
            "valid": False,
        }

    va = projected_volume / previous.volume

    current_start = current.start
    previous_start = previous.start

    current_trades = sum(
        1 for t in ts
        if int(t["time"]) >= current_start
    )

    previous_trades = sum(
        1 for t in ts
        if previous_start <= int(t["time"]) < current_start
    )

    projected_trades = current_trades * time_factor

    if previous_trades <= 0:
        return {
            "va": None,
            "ta": None,
            "valid": False,
        }

    ta = projected_trades / previous_trades

    # Sample-quality guard:
    # very small trade samples can create fake TA/VA spikes.
    if current_trades < 2 or previous_trades < 3:
        return {
            "va": min(max(va, 0.0), 2.0),
            "ta": min(max(ta, 0.0), 2.0),
            "valid": True,
            "low_sample": True,
            "current_trades": current_trades,
            "previous_trades": previous_trades,
        }

    # Normal reliable sample.
    va = min(max(va, 0.0), 10.0)
    ta = min(max(ta, 0.0), 10.0)

    return {
        "va": va,
        "ta": ta,
        "valid": True,
        "low_sample": False,
        "current_trades": current_trades,
        "previous_trades": previous_trades,
    }


def trade_pressure(ts):
    buy = sell = 0.0

    for t in ts:
        price = float(t["price"])
        qty = float(t["qty"])

        # Some API quoteQty values are inconsistent,
        # so calculate trade notional ourselves.
        value = price * qty

        if t.get("isBuyerMaker", False):
            sell += value
        else:
            buy += value

    total = buy + sell

    # Too little two-sided activity should not create a fake strong signal.
    if total <= 0:
        return 1.0

    # If one side is nearly absent, cap the imbalance instead of returning
    # an extreme ratio such as 100x+.
    if sell <= 0:
        return 3.0

    ratio = buy / sell

    # Keep useful imbalance information, but prevent thin markets
    # from dominating Hunt Score.
    return min(max(ratio, 0.0), 5.0)

def book_pressure(depth):
    bids=sum(float(p)*float(q) for p,q in depth.get("bids",[])[:10])
    asks=sum(float(p)*float(q) for p,q in depth.get("asks",[])[:10])
    return clamp((bids/asks-.7)/1.3) if asks else .5

def score(m):
    symbol=m["symbol"]
    ts=get_trades(symbol,1000)
    if len(ts)<10:
        return {
            "symbol": symbol,
            "status": "INSUFFICIENT"
        }

    now=int(time.time()*1000)
    t15=[t for t in ts if now-int(t["time"])<=15*60*1000]
    t1=[t for t in ts if now-int(t["time"])<=60*60*1000]
    t4=[t for t in ts if now-int(t["time"])<=4*60*60*1000]

    # داده کم است، اما اگر فعالیت حداقلی وجود دارد
    # ارز را برای رصد نگه می‌داریم.
    if len(t15)<4 or len(t1)<15:
        if len(t1)>=5 or len(t4)>=20:
            return {
                "symbol": symbol,
                "status": "WATCH",
                "trades15": len(t15),
                "trades1h": len(t1),
                "trades4h": len(t4)
            }
        return {
            "symbol": symbol,
            "status": "INSUFFICIENT",
            "trades15": len(t15),
            "trades1h": len(t1),
            "trades4h": len(t4)
        }

    # Use the 4h trade window so 15m breakout/compression
    # and 1h structure have enough historical candles.
    c15=candles(t4,15*60*1000)
    c1=candles(t4,60*60*1000)
    c4=candles(t4,4*60*60*1000)
    if len(c15)<2: return None

    vol = volume_profile(c15)
    vr = vol["vr"]
    current_volume = vol["volume"]

    activity = activity_profile(t1, c15)

    if not activity.get("valid", False):
        return {
            "symbol": symbol,
            "status": "WATCH",
            "reason": "ACTIVITY_DATA_NOT_READY",
            "trades15": len(t15),
            "trades1h": len(t1),
            "trades4h": len(t4),
        }

    va = activity["va"]
    ta = activity["ta"]

    p15=pct(c15[-1].close,c15[-2].close)
    bs=trade_pressure(t15)
    bo=breakout(c15)
    book=book_pressure(get_depth(symbol,50))

    # Activity / buying pressure is rising.
    activity_signal = (
        va >= 1.2 or
        ta >= 1.2 or
        bs >= 1.2 or
        book >= 0.65
    )

    # Strong accumulation BEFORE price confirmation.
    # This is the key pre-pump fingerprint we want to preserve.
    strong_accumulation = (
        -0.8 <= p15 <= 0.5
        and (va >= 1.8 or ta >= 1.8)
        and (bs >= 1.3 or book >= 0.65)
    )

    # Weak accumulation remains WATCH only.
    # Strong accumulation is allowed to continue into PRE_EARLY logic.
    if p15 <= 0 and activity_signal and not strong_accumulation:
        return {
            "symbol": symbol,
            "status": "WATCH_ACCUMULATION",
            "reason": "WAITING_FOR_PRICE_CONFIRMATION",
            "price": round(c15[-1].close, 8),
            "volume": round(current_volume, 2),
            "p15": round(p15, 2),
            "va": round(va, 2),
            "ta": round(ta, 2),
            "bs": round(bs, 2),
            "book": round(book, 2),
            "breakout": bool(bo),
            "trades15": len(t15),
            "trades1h": len(t1),
            "trades4h": len(t4),
        }

    # EARLY: price has already started confirming the move.
    price_early = 0.05 <= p15 <= 3.0

    # PRE_EARLY: price may still be flat or slightly negative.
    price_pre_early = -0.8 <= p15 <= 0.5

    # Participation/activity acceleration.
    activity_confirmed = (
        va >= 1.5 or
        ta >= 1.5
    )

    # Demand-side confirmation.
    demand_confirmed = (
        (bs >= 1.5 and book >= 0.35)
        or book >= 0.75
    )

    early_confirmed = (
        price_early and
        activity_confirmed and
        demand_confirmed
    )

    pre_early_confirmed = (
        not early_confirmed
        and price_pre_early
        and activity_confirmed
        and (
            strong_accumulation
            or bs >= 1.25
            or book >= 0.65
        )
    )

    # PRE_EARLY / HUNT base score.
    # Balanced for pre-pump hunting: activity + demand + breakout + structure.
    pre_score = 0.0

    # Initial price movement: useful, but must not dominate.
    if price_early:
        pre_score += min(max(p15 / 1.0, 0.0), 1.0) * 20

    # Volume and trade acceleration.
    pre_score += min(max((va - 1.0) / 2.0, 0.0), 1.0) * 20
    pre_score += min(max((ta - 1.0) / 2.0, 0.0), 1.0) * 15

    # Demand confirmation.
    demand_score = max(
        min(max(bs / 1.2, 0.0), 1.0),
        min(max(book / 0.65, 0.0), 1.0)
    )
    pre_score += demand_score * 20

    # 15m breakout is a major confirmation.
    if bo:
        pre_score += 15

    # Multi-timeframe structure confirmation.
    pre_score += structure(c1) * 5
    pre_score += structure(c4) * 5

    # Absolute volume-quality guard:
    # VA/TA can look huge when they rise from a very small base.
    # Penalize very weak VR without completely rejecting early signals.
    vr_penalty = 0.0

    if vr < 0.25:
        vr_penalty = 25.0
    elif vr < 0.50:
        vr_penalty = 15.0
    elif vr < 0.80:
        vr_penalty = 8.0

    pre_score -= vr_penalty

    pre_score = max(0.0, min(100.0, pre_score))

    if early_confirmed:
        signal_status = "EARLY"
    elif pre_early_confirmed:
        signal_status = "PRE_EARLY"
    else:
        signal_status = "SCANNED"

    # Early-Pump score:
    # volume acceleration + trade acceleration must work together.
    volume_score = clamp((va-1)/3) * 15
    trade_score  = clamp((ta-1)/3) * 10

    # If volume rises but trade count does not, reduce the volume contribution.
    if va > 1.5 and ta < 0.75:
        volume_score *= 0.35

    raw=(
        volume_score +
        trade_score +
        clamp(p15/8)*15 +
        bo*15 +
        structure(c1)*10 +
        structure(c4)*10 +
        clamp((bs-1)/1.5)*10 +
        compression(c15)*5 +
        (book-.5)*10
    )
    # ضد تعقیب: اگر همین 15m بیش از 10% جهش کرده، جریمه
    penalty=max(0,p15-10)*1.5
    final=max(0,min(100,raw-penalty))
    return dict(
        symbol=symbol,
        status=signal_status,
        score=round(final, 1),
        pre_score=round(pre_score, 1),
        price=round(c15[-1].close, 8),
        volume=round(current_volume, 2),
        p15=round(p15, 2),
        vr=round(vr, 2),
        va=round(va, 2),
        ta=round(ta, 2),
        bs=round(bs, 2),
        breakout=bool(bo),
        book=round(book, 2)
    )

PERSIST_FILE = os.path.join(os.path.dirname(__file__), "persistence_state.json")

def load_persistence():
    if os.path.exists(PERSIST_FILE):
        try:
            with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_persistence(state):
    tmp = PERSIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, PERSIST_FILE)

DATA_FILE = os.path.join(os.path.dirname(__file__), "tabdeal_radar_v21_data.json")

def save_dashboard_data(out):
    data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    now = int(time.time() * 1000)

    for x in out:
        symbol = x["symbol"]

        row = {
            "time": now,
            "price": x.get("price", 0),
            "volume": x.get("volume", 0),
            "buy_ratio": min(100, max(0, x.get("bs", 0) / 3 * 100)),
            "ob": x.get("book", 0),
            "score": x.get("score"),
            "status": x.get("status", "READY"),
            "pre_score": x.get("pre_score"),
            "hunt_score": x.get("hunt_score"),
            "persistence": x.get("persistence", 0),
            "persistence_bonus": x.get("persistence_bonus", 0),
            "status_bonus": x.get("status_bonus", 0),
            "p15": x.get("p15"),
            "vr": x.get("vr"),
            "va": x.get("va"),
            "bs": x.get("bs"),
            "breakout": x.get("breakout", False),
            "book": x.get("book", 0),
            "trades15": x.get("trades15", 0),
            "trades1h": x.get("trades1h", 0),
            "trades4h": x.get("trades4h", 0)
        }

        history = data.get(symbol, [])
        history.append(row)
        data[symbol] = history[-120:]

    tmp = DATA_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    os.replace(tmp, DATA_FILE)


def run_once(max_markets=0):
    markets=get_markets()
    if max_markets: markets=markets[:max_markets]

    persistence = load_persistence()

    out=[]

    scan_ok = 0
    scan_errors = 0

    for m in markets:
        try:
            s=score(m)

            # score() completed without a network/API exception.
            # INSFFICIENT/WATCH are still valid completed scans.
            scan_ok += 1

            if s:
                symbol = s.get("symbol")
                status = s.get("status")

                if symbol:
                    previous_p = int(persistence.get(symbol, 0) or 0)

                    # Smart persistence:
                    # strengthen on real candidates,
                    # decay gradually on misses instead of resetting to zero.
                    if status in ("PRE_EARLY", "EARLY"):
                        persistence[symbol] = min(previous_p + 1, 10)
                    else:
                        persistence[symbol] = max(previous_p - 1, 0)

                    s["persistence"] = persistence.get(symbol, 0)

                    # Persistence helps confirmation, but must not dominate.
                    # No bonus for weak/no-signal rows.
                    if (
                        status in ("PRE_EARLY", "EARLY")
                        and s.get("pre_score") is not None
                        and float(s.get("pre_score") or 0) >= 40
                    ):
                        persistence_bonus = min(
                            s["persistence"] * 2.0,
                            10.0
                        )
                    else:
                        persistence_bonus = 0.0

                    s["persistence_bonus"] = round(
                        persistence_bonus,
                        1
                    )

                    status_bonus = 0.0
                    if status == "EARLY":
                        status_bonus = 5.0
                    elif status == "PRE_EARLY":
                        status_bonus = 0.0

                    s["status_bonus"] = status_bonus

                    if (
                        status in ("PRE_EARLY", "EARLY")
                        and s.get("pre_score") is not None
                    ):
                        hunt = (
                            s["pre_score"]
                            + persistence_bonus
                            + status_bonus
                        )

                        # PRE_EARLY must never look like a fully confirmed signal.
                        if status == "PRE_EARLY":
                            hunt = min(hunt, 84.0)

                        # No fresh 15m breakout = candidate, not confirmed leader.
                        if not s.get("breakout", False):
                            hunt = min(hunt, 84.0)

                        # First EARLY hit is only an alert, not a confirmed leader.
                        # Require at least 2 consecutive candidate scans for full Hunt score.
                        if (
                            status == "EARLY"
                            and int(s.get("persistence", 0) or 0) < 2
                        ):
                            hunt = min(hunt, 78.0)

                        s["hunt_score"] = round(
                            min(100.0, hunt),
                            1
                        )
                    else:
                        s["hunt_score"] = None

                out.append(s)
        except Exception as e:
            scan_errors += 1
            print("skip", m.get("symbol"), e)

    total_markets = len(markets)

    health_ratio = (
        scan_ok / total_markets
        if total_markets > 0
        else 0.0
    )

    print(
        f"\\nSCAN HEALTH: "
        f"{scan_ok}/{total_markets} OK "
        f"({health_ratio * 100:.1f}%) | "
        f"errors={scan_errors}"
    )

    # Protect dashboard/history from incomplete scans.
    # If more than 15% of markets failed, do not persist this scan.
    if total_markets > 0 and health_ratio < 0.85:
        print(
            "⚠️ SCAN REJECTED: API/network quality too low. "
            "Dashboard and persistence were NOT updated."
        )
        return

    save_persistence(persistence)

    # Only healthy scans are allowed into dashboard/history.
    save_dashboard_data(out)

    # Terminal ranking must contain only true hunt candidates.
    candidates = [
        x for x in out
        if x.get("status") in ("EARLY", "PRE_EARLY")
        and x.get("hunt_score") is not None
    ]

    candidates.sort(
        key=lambda x: x.get("hunt_score", 0),
        reverse=True
    )

    print("\n"+"="*78)
    print("TABDEAL PRE-PUMP RADAR v2 | PUBLIC DATA | NO ORDERS")
    print("="*78)
    for i,s in enumerate(candidates[:10],1):
        mark="🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else "  "

        if s.get("score") is None:
            print(
                f"{mark} {i:02d} {s['symbol']:<14} "
                f"WATCH | 15m={s.get('trades15',0)} "
                f"1h={s.get('trades1h',0)} "
                f"4h={s.get('trades4h',0)}"
            )
        else:
            print(
                f"{mark} {i:02d} {s['symbol']:<14} "
                f"Hunt={s['hunt_score']:>5.1f}/100 "
                f"15m={s['p15']:+6.2f}% "
                f"V={s['vr']:.1f}x "
                f"VA={s['va']:.1f}x "
                f"TA={s['ta']:.1f}x "
                f"B/S={s['bs']:.1f} "
                f"Book={s['book']:.2f} "
                f"BO={'Y' if s['breakout'] else 'N'} "
                f"P={s.get('persistence',0)} "
                f"{s.get('status','?')}"
            )
    if candidates:
        # انتخاب اصلی = قوی‌ترین Hunt Score واقعی.
        # EARLY و PRE_EARLY هر دو در یک رتبه‌بندی قرار دارند.
        best = candidates[0]

        print(
            "\n🥇 انتخاب اصلی:",
            best["symbol"],
            f"Hunt={best['hunt_score']:.1f}/100",
            f"Status={best['status']}"
        )

        if best.get("hunt_score", 0) >= 80 and best.get("status") in ("EARLY", "PRE_EARLY"):
            send_ntfy(
                f"{best['symbol']} | Hunt={best['hunt_score']:.1f}/100 | "
                f"Status={best['status']} | "
                f"15m={best.get('p15', 0):+.2f}% | "
                f"V={best.get('vr', 0):.1f}x | "
                f"P={best.get('persistence', 0)}",
                title="Tabdeal Radar Alert"
            )
    else:
        print("\nداده کافی برای شکار EARLY / PRE_EARLY وجود ندارد.")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--once",action="store_true")
    p.add_argument("--interval",type=int,default=60)
    p.add_argument("--max-markets",type=int,default=0)
    a=p.parse_args()
    if a.once:
        run_once(a.max_markets); return
    while True:
        try: run_once(a.max_markets)
        except Exception as e: print("RADAR ERROR:",e)
        time.sleep(max(15,a.interval))

if __name__=="__main__":
    main()
