# Tabdeal Pre-Pump Radar v2
# Public market data only; NO order placement.

import argparse, time, statistics, json, os
from hunt_entry_v1_shadow import register_entry_v1, update_entry_v1_outcomes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import requests
from breakout_observer_v1 import update_breakout_observer
from ignition_history_logger import save_ignition_history
from move_early_v2_shadow import run_shadow as run_move_early_v2_shadow
from hunt_quiet_wake_v1 import update_quiet_wake, update_quiet_wake_outcomes, update_quiet_wake_snapshots

BASE = "https://api1.tabdeal.org"
TIMEOUT = 15

NTFY_TOPIC = "tabdeal-radar-kian-8264"
NTFY_MOVE_TOPIC = "tabdeal-move-kian-8264"

NTFY_CIRCUIT_OPEN = False

def send_ntfy(message, title="Tabdeal Radar", topic=None):
    global NTFY_CIRCUIT_OPEN

    if NTFY_CIRCUIT_OPEN:
        print("NTFY: skipped | circuit open |", title)
        return False

    url = f"https://ntfy.sh/{topic or NTFY_TOPIC}"
    if "MOVE UP" in title:
        priority = "default"
        tags = "green_circle,chart_with_upwards_trend"
    elif "PULLBACK" in title:
        priority = "default"
        tags = "red_circle,chart_with_downwards_trend"
    else:
        priority = "high"
        tags = "dart,fire"

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags
    }

    for attempt in range(2):
        try:
            r = requests.post(
                url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=10
            )

            if r.status_code == 429:
                if attempt == 0:
                    retry_after = r.headers.get("Retry-After", "3")
                    try:
                        wait_s = float(retry_after)
                    except (TypeError, ValueError):
                        wait_s = 3.0

                    wait_s = max(1.0, min(wait_s, 10.0))
                    print(f"NTFY 429: retry in {wait_s:.1f}s")
                    time.sleep(wait_s)
                    continue

                print("NTFY 429: retry exhausted | circuit opened")
                NTFY_CIRCUIT_OPEN = True
                return False

            r.raise_for_status()

            print(
                "NTFY: sent |",
                title,
                "|",
                message.replace("\n", " ")
            )
            return True

        except requests.RequestException as e:
            print("NTFY error:", e)
            return False

    return False


ALERT_HISTORY_FILE = os.path.join(
    os.path.dirname(__file__),
    "radar_alert_history.json"
)

def save_alert_history(best):
    history = []

    if os.path.exists(ALERT_HISTORY_FILE):
        try:
            with open(ALERT_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    row = {
        "time": int(time.time() * 1000),
        "symbol": best.get("symbol"),
        "price": best.get("price"),
        "hunt_score": best.get("hunt_score"),
        "status": best.get("status"),
        "p15": best.get("p15"),
        "vr": best.get("vr"),
        "va": best.get("va"),
        "bs": best.get("bs"),
        "breakout": best.get("breakout"),
        "persistence": best.get("persistence")
    }

    history.append(row)

    # نگهداری 1000 هشدار آخر
    history = history[-1000:]

    tmp = ALERT_HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    os.replace(tmp, ALERT_HISTORY_FILE)


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

    excluded = {
        # Majors / stable assets excluded from Shadow V13 universe.
        "BTCIRT", "ETHIRT", "BNBIRT", "SOLIRT",
        "XRPIRT", "ADAIRT", "DOGEIRT",
        "USDTIRT", "USDCIRT", "DAIIRT",
        "FDUSDIRT", "TUSDIRT", "PAXGIRT",

        # Existing radar exclusions retained.
        "TRXIRT", "XRDIRT"
    }

    return [
        m for m in data
        if m.get("status") == "TRADING"
        and "SPOT" in m.get("permissions", ["SPOT"])
        and str(m.get("symbol", "")).endswith("IRT")
        and str(m.get("symbol", "")) not in excluded
    ]

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
    ts=sorted(ts, key=lambda t: int(t["time"]))
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
                "price": float(ts[-1]["price"]),
                "volume": round(sum(float(t.get("quoteQty", float(t["price"])*float(t["qty"]))) for t in t15), 2),
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
    # Demand confirmation:
    # Normal path uses trade pressure + order book.
    # Power-breakout path prevents a very strong move from being rejected
    # only because the instantaneous order book is marginally below threshold.
    power_breakout_confirmed = (
        bo
        and vr >= 3.0
        and va >= 3.0
        and bs >= 1.5
    )

    demand_confirmed = (
        (bs >= 1.5 and book >= 0.35)
        or book >= 0.75
        or power_breakout_confirmed
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
        book=round(book, 2),
        trades15=len(t15),
        trades1h=len(t1),
        trades4h=len(t4),
        structure1h=round(structure(c1), 3),
        structure4h=round(structure(c4), 3),
        compression15=round(compression(c15), 3)
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
IGNITION_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "ignition_history.json")

def sequence_score_v0(history, current):
    recent = history[-6:]
    records = recent + [current]
    score = 0.0
    flags = []

    cand = sum(1 for r in records if r.get("status") in ("EARLY", "PRE_EARLY"))
    accum = sum(1 for r in records if r.get("status") == "WATCH_ACCUMULATION")
    insuff = sum(1 for r in recent if r.get("status") == "INSUFFICIENT")

    if cand:
        score += min(cand * 8.0, 24.0); flags.append("candidate_repeat")
    if accum:
        score += min(accum * 6.0, 18.0); flags.append("accumulation")

    vr_max = max([float(r.get("vr") or 0) for r in records] or [0])
    va_max = max([float(r.get("va") or 0) for r in records] or [0])
    bs_max = max([float(r.get("bs") or 0) for r in records] or [0])

    if vr_max >= 3: score += 12; flags.append("vr3")
    elif vr_max >= 1: score += 6; flags.append("vr1")

    if va_max >= 3: score += 10; flags.append("va3")
    elif va_max >= 1.5: score += 5; flags.append("va15")

    if bs_max >= 2: score += 8; flags.append("bs2")
    elif bs_max >= 1: score += 4; flags.append("bs1")

    if any(bool(r.get("breakout")) for r in records):
        score += 10; flags.append("breakout")

    s1_max = max([float(r.get("structure1h") or 0) for r in records] or [0])
    if s1_max >= 0.8: score += 10; flags.append("structure1h")
    elif s1_max >= 0.6: score += 5

    trades = [int(r.get("trades15") or 0) for r in records]
    if len(trades) >= 3 and trades[-1] > trades[-2] >= trades[-3] and trades[-1] >= 10:
        score += 8; flags.append("trade_accel")

    if insuff >= 3:
        score -= 12; flags.append("thin_history")

    p15 = float(current.get("p15") or 0)
    if p15 > 5:
        score -= min((p15 - 5) * 2, 15); flags.append("anti_chase")

    return round(max(0.0, min(100.0, score)), 1), flags


def sequence_score_v1(history, current):
    recent = history[-6:]
    records = recent + [current]
    weights = [0.20, 0.30, 0.45, 0.60, 0.80, 0.95, 1.00][-len(records):]
    score = 0.0
    flags = []

    def f(r, k):
        try:
            return float(r.get(k) or 0)
        except Exception:
            return 0.0

    candidate_w = sum(w for r, w in zip(records, weights) if r.get("status") in ("EARLY", "PRE_EARLY"))
    accum_w = sum(w for r, w in zip(records, weights) if r.get("status") == "WATCH_ACCUMULATION")

    if candidate_w >= 1.0:
        score += min(candidate_w * 10.0, 24.0); flags.append("recent_candidates")
    if accum_w >= 0.8:
        score += min(accum_w * 7.0, 16.0); flags.append("recent_accumulation")

    power = 0.0
    for r, w in zip(records, weights):
        local = 0.0
        if f(r,"vr") >= 3: local += 4
        elif f(r,"vr") >= 1: local += 2
        if f(r,"va") >= 3: local += 3
        elif f(r,"va") >= 1.5: local += 1.5
        if f(r,"bs") >= 2: local += 2
        elif f(r,"bs") >= 1: local += 1
        power += local * w
    score += min(power, 24.0)
    if power >= 8: flags.append("recent_power")

    recent3 = records[-3:]
    bo_count = sum(1 for r in recent3 if bool(r.get("breakout")))
    s1_count = sum(1 for r in recent3 if f(r,"structure1h") >= 0.8)

    if bo_count >= 2:
        score += 12; flags.append("breakout_continuity")
    elif bo_count == 1:
        score += 5

    if s1_count >= 2:
        score += 12; flags.append("structure_continuity")
    elif s1_count == 1:
        score += 5

    t = [int(r.get("trades15") or 0) for r in records[-4:]]
    if len(t) >= 3 and t[-1] >= 10 and t[-1] >= t[-2] and t[-2] >= t[-3]:
        score += 8; flags.append("trade_persistence")

    strong_old = any(
        r.get("status") in ("EARLY","PRE_EARLY")
        and (f(r,"vr") >= 3 or f(r,"va") >= 3)
        for r in records[:-2]
    )

    latest_measured = None
    for r in reversed(records[-3:]):
        if (
            r.get("vr") is not None
            or r.get("structure1h") is not None
            or r.get("breakout") is True
        ):
            latest_measured = r
            break

    recent_confirm = False
    if latest_measured is not None:
        recent_confirm = (
            bool(latest_measured.get("breakout"))
            or f(latest_measured,"structure1h") >= 0.8
        )

    if strong_old and recent_confirm:
        score += 12; flags.append("reignition")

    cur_bo = bool(current.get("breakout"))
    cur_t15 = int(current.get("trades15") or 0)

    has_vr = current.get("vr") is not None
    has_bs = current.get("bs") is not None
    has_s1 = current.get("structure1h") is not None

    if has_vr and has_bs and has_s1:
        cur_vr = f(current,"vr")
        cur_bs = f(current,"bs")
        cur_s1 = f(current,"structure1h")

        if cur_vr < 0.2 and cur_bs < 0.5 and cur_s1 < 0.3 and not cur_bo:
            score -= 25; flags.append("current_fade")
        elif cur_t15 <= 3 and cur_vr < 0.5 and not cur_bo:
            score -= 12; flags.append("activity_fade")

    p15 = f(current,"p15")
    if p15 > 5:
        score -= min((p15 - 5) * 2.0, 15.0); flags.append("anti_chase")

    return round(max(0.0, min(100.0, score)), 1), flags



# ===== STRONG WAKE FORWARD LEDGER v1 =====

STRONG_WAKE_LEDGER_FILE = "strong_wake_events.json"
STRONG_WAKE_HORIZON_MS = 12 * 60 * 60 * 1000
STRONG_WAKE_HIT_PCT = 10.0
STRONG_WAKE_STOP_PCT = -5.0


def load_strong_wake_ledger():
    try:
        with open(STRONG_WAKE_LEDGER_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
            return x if isinstance(x, list) else []
    except Exception:
        return []


def save_strong_wake_ledger(events):
    tmp = STRONG_WAKE_LEDGER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STRONG_WAKE_LEDGER_FILE)


def update_strong_wake_ledger(data, now_ms):
    events = load_strong_wake_ledger()

    def find_existing_strong_event(symbol, event_time, anchor_time):
        for e in events:
            if e.get("symbol") != symbol:
                continue

            old_anchor = e.get("anchor_time")

            # Best dedup key: same Wake anchor.
            if anchor_time is not None and old_anchor is not None:
                try:
                    if int(anchor_time) == int(old_anchor):
                        return e
                except Exception:
                    pass

                # Different known anchors are different waves.
                continue

            # Legacy fallback: one side has no anchor metadata.
            old_time = int(e.get("event_time", 0) or 0)

            if (
                old_time > 0
                and abs(event_time - old_time) <= 2 * 60 * 60 * 1000
            ):
                return e

        return None

    # Discover every historical strong_wake row not yet in ledger.
    for symbol, history in data.items():
        if not isinstance(history, list):
            continue

        for r in history:
            if not r.get("strong_wake"):
                continue

            event_time = int(r.get("time", 0) or 0)
            event_price = float(r.get("price") or 0)

            if event_time <= 0 or event_price <= 0:
                continue

            anchor_time = r.get("wake_anchor_time")

            existing_event = find_existing_strong_event(
                symbol,
                event_time,
                anchor_time
            )

            if existing_event is not None:
                # Enrich an older legacy event when metadata becomes available.
                if (
                    existing_event.get("anchor_time") is None
                    and anchor_time is not None
                ):
                    existing_event["anchor_time"] = anchor_time
                    existing_event["anchor_price"] = r.get("wake_anchor_price")
                    existing_event["anchor_kind"] = r.get("wake_anchor_kind")
                    existing_event["legacy"] = False

                continue

            events.append({
                "symbol": symbol,
                "event_time": event_time,
                "event_price": event_price,
                "anchor_time": r.get("wake_anchor_time"),
                "anchor_price": r.get("wake_anchor_price"),
                "anchor_kind": r.get("wake_anchor_kind"),
                "fresh_ratio": r.get("wake_fresh_ratio"),

                # Trade Momentum v1 - frozen at Strong Wake event
                "tm_e15_e1": r.get("trade_momentum_e15_e1"),
                "tm_e1_e4": r.get("trade_momentum_e1_e4"),
                "tm_g15": r.get("trade_momentum_g15"),
                "tm_g1": r.get("trade_momentum_g1"),
                "tm_g4": r.get("trade_momentum_g4"),

                "legacy": r.get("wake_anchor_time") is None,
                "result": "PENDING",
                "result_time": None,
                "result_move": None,
                "max_move": 0.0,
                "min_move": 0.0,
                "ledger_version": "wake-ledger-v1"
            })


    # Update unresolved events from currently retained scan history.
    for e in events:
        # HIT/STOP first-touch stays locked, but keep tracking
        # MAX/MIN until the full 24h research horizon ends.
        if e.get("result") == "EXPIRED_NONE":
            continue

        symbol = e.get("symbol")
        t0 = int(e.get("event_time", 0) or 0)
        p0 = float(e.get("event_price") or 0)

        if t0 <= 0 or p0 <= 0:
            continue

        history = data.get(symbol, [])
        max_move = e.get("max_move")
        min_move = e.get("min_move")

        try:
            max_move = float(max_move)
        except Exception:
            max_move = 0.0

        try:
            min_move = float(min_move)
        except Exception:
            min_move = 0.0

        candidates = []

        for r in history:
            t = int(r.get("time", 0) or 0)

            if t <= t0 or t > t0 + STRONG_WAKE_HORIZON_MS:
                continue

            price = float(r.get("price") or 0)

            if price <= 0:
                continue

            candidates.append((t, price))

        candidates.sort()

        for t, price in candidates:
            move = 100.0 * (price / p0 - 1.0)

            max_move = max(max_move, move)
            min_move = min(min_move, move)

            if move >= STRONG_WAKE_HIT_PCT:
                e["result"] = "HIT_FIRST"
                e["result_time"] = t
                e["result_move"] = round(move, 4)
                break

            if move <= STRONG_WAKE_STOP_PCT:
                e["result"] = "STOP_FIRST"
                e["result_time"] = t
                e["result_move"] = round(move, 4)
                break

        e["max_move"] = round(max_move, 4)
        e["min_move"] = round(min_move, 4)

        if (
            e.get("result") == "PENDING"
            and now_ms >= t0 + STRONG_WAKE_HORIZON_MS
        ):
            e["result"] = "EXPIRED_NONE"

    events.sort(
        key=lambda e: (
            int(e.get("event_time", 0)),
            str(e.get("symbol", ""))
        )
    )

    save_strong_wake_ledger(events)
    return events



# ===== PRE-WAKE FORWARD LEDGER v1 - RESEARCH ONLY =====
PRE_WAKE_LEDGER_FILE = "pre_wake_events.json"
PRE_WAKE_HORIZON_MS = 24 * 60 * 60 * 1000
PRE_WAKE_HIT_PCT = 10.0
PRE_WAKE_STOP_PCT = -5.0


def load_pre_wake_ledger():
    try:
        with open(PRE_WAKE_LEDGER_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def save_pre_wake_ledger(events):
    tmp = PRE_WAKE_LEDGER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRE_WAKE_LEDGER_FILE)


def update_pre_wake_ledger(data, now_ms):
    events = load_pre_wake_ledger()

    existing = {
        (str(e.get("symbol", "")), int(e.get("event_time", 0) or 0))
        for e in events
    }

    # Discover historical PRE-WAKE V1 events still retained in history.
    for symbol, history in data.items():
        if not isinstance(history, list):
            continue

        for r in history:
            if not r.get("pre_wake_v1"):
                continue

            event_time = int(r.get("time", 0) or 0)
            event_price = float(r.get("price") or 0)

            if event_time <= 0 or event_price <= 0:
                continue

            key = (symbol, event_time)
            if key in existing:
                continue

            events.append({
                "symbol": symbol,
                "event_time": event_time,
                "event_price": event_price,

                # Frozen research context at PRE-WAKE event.
                "status": r.get("status"),
                "sequence_score_v1": r.get("sequence_score_v1"),
                "p15": r.get("p15"),
                "vr": r.get("vr"),
                "va": r.get("va"),
                "ta": r.get("ta"),
                "bs": r.get("bs"),
                "book": r.get("book"),
                "breakout": r.get("breakout"),
                "fast_v13": r.get("fast_v13"),

                "result": "PENDING",
                "result_time": None,
                "result_move": None,
                "max_move": 0.0,
                "min_move": 0.0,
                "ledger_version": "pre-wake-ledger-v1"
            })

            existing.add(key)

    # Evaluate in chronological order: +10 FIRST versus -5 FIRST.
    for e in events:
        if e.get("result") != "PENDING":
            continue

        symbol = e.get("symbol")
        t0 = int(e.get("event_time", 0) or 0)
        p0 = float(e.get("event_price") or 0)

        if t0 <= 0 or p0 <= 0:
            continue

        history = data.get(symbol, [])

        try:
            max_move = float(e.get("max_move", 0) or 0)
        except Exception:
            max_move = 0.0

        try:
            min_move = float(e.get("min_move", 0) or 0)
        except Exception:
            min_move = 0.0

        candidates = []

        for r in history:
            t = int(r.get("time", 0) or 0)
            if t <= t0 or t > t0 + PRE_WAKE_HORIZON_MS:
                continue

            price = float(r.get("price") or 0)
            if price <= 0:
                continue

            candidates.append((t, price))

        candidates.sort()

        first_result = e.get("result") != "PENDING"

        for t, price in candidates:
            move = 100.0 * (price / p0 - 1.0)

            max_move = max(max_move, move)
            min_move = min(min_move, move)

            if not first_result:
                if move >= PRE_WAKE_HIT_PCT:
                    e["result"] = "HIT_FIRST"
                    e["result_time"] = t
                    e["result_move"] = round(move, 4)
                    first_result = True

                elif move <= PRE_WAKE_STOP_PCT:
                    e["result"] = "STOP_FIRST"
                    e["result_time"] = t
                    e["result_move"] = round(move, 4)
                    first_result = True

        e["max_move"] = round(max_move, 4)
        e["min_move"] = round(min_move, 4)

        if (
            e.get("result") == "PENDING"
            and now_ms >= t0 + PRE_WAKE_HORIZON_MS
        ):
            e["result"] = "EXPIRED_NONE"

    events.sort(
        key=lambda e: (
            int(e.get("event_time", 0)),
            str(e.get("symbol", ""))
        )
    )

    save_pre_wake_ledger(events)
    return events



# ===== MOVE TRACKER V1 =====
# Persistent wave/milestone tracker.
# Independent from Hunt, PRE-WAKE and Strong-Wake scoring.

MOVE_TRACKER_FILE = "move_tracker_state.json"
MOVE_TRACKER_ARCHIVE_FILE = "move_tracker_archive.json"

MOVE_UP_LEVELS = (
    5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80,
    90, 100, 110, 120, 130, 140, 150
)

MOVE_DD_LEVELS = (
    5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90
)

MOVE_RESET_MIN_AGE_MS = 12 * 60 * 60 * 1000
MOVE_RESET_DRAWDOWN_PCT = 10.0


def load_move_tracker_state():
    try:
        with open(MOVE_TRACKER_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def save_move_tracker_state(state):
    tmp = MOVE_TRACKER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MOVE_TRACKER_FILE)


def load_move_tracker_archive():
    try:
        with open(MOVE_TRACKER_ARCHIVE_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def archive_move_tracker_wave(wave, reset_time, reset_price, new_trigger_kind):
    archive = load_move_tracker_archive()

    item = dict(wave)
    item["closed_time"] = reset_time
    item["closed_price"] = reset_price
    item["close_reason"] = "NEW_WAKE_AFTER_DRAWDOWN"
    item["next_trigger_kind"] = new_trigger_kind

    archive.append(item)

    # Safety cap; preserves the most recent completed waves.
    archive = archive[-5000:]

    tmp = MOVE_TRACKER_ARCHIVE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MOVE_TRACKER_ARCHIVE_FILE)


def move_tracker_context(row):
    return {
        "status": row.get("status"),
        "vr": row.get("vr"),
        "va": row.get("va"),
        "bs": row.get("bs"),
        "breakout": row.get("breakout"),
        "fast_shadow": row.get("fast_shadow"),
        "fast_kind": row.get("fast_kind"),
        "fast_active": row.get("fast_active"),
        "confirmed_fast": row.get("confirmed_fast"),
        "fast_anchor_time": row.get("fast_anchor_time"),
        "fast_anchor_price": row.get("fast_anchor_price"),
        "sequence_score_v1": row.get("sequence_score_v1"),

        # Raw anchor features - research/logging only.
        # No effect on Wake, Hunt, status, alerts, or trading logic.
        "trades15": row.get("trades15"),
        "trades1h": row.get("trades1h"),
        "trades4h": row.get("trades4h"),
        "wake_fresh_ratio": row.get("wake_fresh_ratio"),
        "wake_a1": row.get("wake_a1"),
        "wake_a4": row.get("wake_a4"),
        "wake_prev_gap_min": row.get("wake_prev_gap_min"),
        "wake_prev_valid": row.get("wake_prev_valid"),
        "p15": row.get("p15"),
        "book": row.get("book"),
    }


def update_move_tracker(data, now_ms, alerts_enabled=False):
    state = load_move_tracker_state()

    for symbol, history in data.items():
        if not isinstance(history, list) or not history:
            continue

        row = history[-1]
        row_time = int(row.get("time", 0) or 0)
        price = float(row.get("price") or 0)

        if row_time <= 0 or price <= 0:
            continue

        wave = state.get(symbol)

        # Start only from a fresh forward trigger.
        trigger_kind = None
        trigger_price = None
        trigger_time = None

        if row.get("wake_short") or row.get("wake_deep"):
            trigger_kind = (
                "WAKE_DEEP"
                if row.get("wake_deep")
                else "WAKE_SHORT"
            )
            trigger_price = price
            trigger_time = row_time

        elif row.get("strong_wake") and row.get("wake_anchor_price"):
            trigger_kind = "STRONG_WAKE"
            trigger_price = float(row.get("wake_anchor_price") or 0)
            trigger_time = int(row.get("wake_anchor_time") or row_time)

        # Reset an existing wave only on a fresh new wake,
        # after >=12h AND >=10% drawdown from the old peak.
        if wave is not None and trigger_kind is not None and trigger_price > 0:
            old_anchor_time = int(wave.get("anchor_time") or 0)
            old_peak_price = float(
                wave.get("peak_price")
                or wave.get("anchor_price")
                or 0
            )

            wave_age_ms = max(0, row_time - old_anchor_time)
            old_peak_dd_pct = (
                100.0 * (price / old_peak_price - 1.0)
                if old_peak_price > 0
                else 0.0
            )

            trigger_age_ms = max(0, now_ms - int(trigger_time or 0))

            if (
                trigger_age_ms <= 30 * 60 * 1000
                and wave_age_ms >= MOVE_RESET_MIN_AGE_MS
                and old_peak_dd_pct <= -MOVE_RESET_DRAWDOWN_PCT
            ):
                # Preserve the completed wave before starting a new one.
                archive_move_tracker_wave(
                    wave,
                    reset_time=row_time,
                    reset_price=price,
                    new_trigger_kind=trigger_kind,
                )
                wave = None

        if wave is None:
            if trigger_kind is None or trigger_price <= 0:
                continue

            # New waves may start only from a fresh trigger.
            # Prevent old retained history from creating a wave after restart.
            trigger_age_ms = max(0, now_ms - int(trigger_time or 0))
            if trigger_age_ms > 30 * 60 * 1000:
                continue

            wave = {
                "symbol": symbol,
                "anchor_time": trigger_time,
                "anchor_price": trigger_price,
                "anchor_kind": trigger_kind,
                "created_time": now_ms,

                "peak_price": price,
                "peak_time": row_time,

                "last_price": price,
                "last_time": row_time,

                "max_gain_pct": round(
                    100.0 * (price / trigger_price - 1.0), 4
                ),
                "drawdown_from_peak_pct": 0.0,

                "up_levels_sent": [],
                "dd_levels_sent": [],
                "up_levels_notified": [],
                "dd_levels_notified": [],

                "anchor_context": move_tracker_context(row),
                "last_context": move_tracker_context(row),

                "tracker_version": "move-tracker-v1"
            }

            state[symbol] = wave

        anchor_price = float(wave.get("anchor_price") or 0)
        peak_price = float(wave.get("peak_price") or anchor_price)

        if anchor_price <= 0:
            continue

        # Update peak.
        if price > peak_price:
            peak_price = price
            wave["peak_price"] = price
            wave["peak_time"] = row_time

        gain_pct = 100.0 * (price / anchor_price - 1.0)
        dd_pct = (
            100.0 * (price / peak_price - 1.0)
            if peak_price > 0
            else 0.0
        )

        wave["last_price"] = price
        wave["last_time"] = row_time
        wave["max_gain_pct"] = round(
            max(float(wave.get("max_gain_pct") or 0), gain_pct), 4
        )
        wave["drawdown_from_peak_pct"] = round(dd_pct, 4)
        wave["last_context"] = move_tracker_context(row)

        up_sent = set(int(x) for x in wave.get("up_levels_sent", []))
        dd_sent = set(int(x) for x in wave.get("dd_levels_sent", []))

        up_notified = set(
            int(x) for x in wave.get("up_levels_notified", [])
        )
        dd_notified = set(
            int(x) for x in wave.get("dd_levels_notified", [])
        )

        # Growth milestones from fixed anchor.
        for level in MOVE_UP_LEVELS:
            if gain_pct + 1e-9 >= level and level not in up_sent:
                up_sent.add(level)

                wave.setdefault("milestone_events", []).append({
                    "type": "UP",
                    "level": level,
                    "time": row_time,
                    "price": price,
                    "anchor_price": anchor_price,
                    "peak_price": peak_price,
                    "gain_pct": round(gain_pct, 4),
                    "drawdown_from_peak_pct": round(dd_pct, 4),
                    "context": move_tracker_context(row),
                })

        # Pullback milestones from wave peak.
        # A decline is considered a pullback only after the wave
        # has previously achieved at least +5% from its anchor.
        drawdown_abs = max(0.0, -dd_pct)
        peak_gain_pct = 100.0 * (peak_price / anchor_price - 1.0)

        for level in MOVE_DD_LEVELS:
            if (
                peak_gain_pct >= 5.0
                and drawdown_abs + 1e-9 >= level
                and level not in dd_sent
            ):
                dd_sent.add(level)

                wave.setdefault("milestone_events", []).append({
                    "type": "PULLBACK",
                    "level": level,
                    "time": row_time,
                    "price": price,
                    "anchor_price": anchor_price,
                    "peak_price": peak_price,
                    "gain_pct": round(gain_pct, 4),
                    "drawdown_from_peak_pct": round(dd_pct, 4),
                    "context": move_tracker_context(row),
                })

        # Phone delivery is independent from milestone discovery.
        # A failed ntfy delivery remains eligible for retry next scan.
        if alerts_enabled:
            for level in sorted(up_sent - up_notified):
                ok = send_ntfy(
                    f"{symbol} | +{level}% from anchor | "
                    f"Price={price:.8g} | Anchor={anchor_price:.8g} | "
                    f"Peak={peak_price:.8g}",
                    title="Tabdeal MOVE UP", topic=NTFY_MOVE_TOPIC
                )
                if ok:
                    up_notified.add(level)

            for level in sorted(dd_sent - dd_notified):
                ok = send_ntfy(
                    f"{symbol} | -{level}% from peak | "
                    f"Price={price:.8g} | Peak={peak_price:.8g} | "
                    f"Anchor={anchor_price:.8g} | "
                    f"Gain={gain_pct:+.1f}%",
                    title="Tabdeal MOVE PULLBACK", topic=NTFY_MOVE_TOPIC
                )
                if ok:
                    dd_notified.add(level)

        wave["up_levels_sent"] = sorted(up_sent)
        wave["dd_levels_sent"] = sorted(dd_sent)
        wave["up_levels_notified"] = sorted(up_notified)
        wave["dd_levels_notified"] = sorted(dd_notified)

    save_move_tracker_state(state)
    return state


# ===== SHADOW V13 FAST/FOLLOW-THROUGH =====

FAST_V13_WAVE_MS = 6 * 60 * 60 * 1000
FAST_V13_CONFIRM30_MS = 30 * 60 * 1000
FAST_V13_CONFIRM45_MS = 45 * 60 * 1000


def _v13_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def classify_fast_v13(r):
    """
    Fingerprints found in historical backtest.

    BREAKOUT / VOL:
        require 30m follow-through.

    ACC:
        require 45m follow-through.

    THIN:
        experimental watch only; never strong-confirmed.
    """
    p15 = _v13_float(r.get("p15"))
    vr = _v13_float(r.get("vr"))
    va = _v13_float(r.get("va"))
    bs = _v13_float(r.get("bs"))
    bo = bool(r.get("breakout"))
    st = r.get("status", "")

    if p15 is None:
        return None

    # A) FAST BREAKOUT — VTHO / ARK style
    if (
        bo
        and -0.5 <= p15 <= 3.6
        and va is not None and va >= 2
        and (
            (vr is not None and vr >= 0.5)
            or
            (bs is not None and bs >= 4)
        )
    ):
        return "FAST_BREAKOUT"

    # B) FAST ACCUMULATION — REZ style
    if (
        -1.6 <= p15 <= 0
        and st in ("WATCH_ACCUMULATION", "EARLY", "PRE_EARLY")
        and va is not None and va >= 7
        and bs is not None and bs >= 2
    ):
        return "FAST_ACC"

    # C) FAST VOLUME IGNITION — STEEM style
    if (
        -0.8 <= p15 <= 1.5
        and st == "EARLY"
        and va is not None and va >= 9
        and vr is not None and vr >= 1
    ):
        return "FAST_VOL"

    # D) THIN MARKET — FLOCK style, experimental only
    if (
        1.0 <= p15 <= 3.0
        and st == "EARLY"
        and va is not None and va >= 9
        and bs is not None and bs >= 4
        and vr is not None and vr <= 0.05
    ):
        return "THIN"

    return None


def evaluate_fast_v13(history, current, now_ms):
    """
    Reconstruct first signal of each 6h wave from stored history.

    Follow-through:
      BREAKOUT/VOL -> 30m
      ACC          -> 45m

    Confirmation condition from backtest:
      min move >= -3%
      max move >= +1%
    """

    cur = dict(current)
    cur["time"] = now_ms

    records = list(history) + [cur]
    records = sorted(
        (
            r for r in records
            if r.get("time") is not None
        ),
        key=lambda r: int(r.get("time", 0))
    )

    candidates = []

    for r in records:
        kind = classify_fast_v13(r)
        price = _v13_float(r.get("price"))

        if kind and price is not None and price > 0:
            candidates.append(
                (
                    int(r.get("time", 0)),
                    kind,
                    price
                )
            )

    # Reproduce "first signal of each 6h wave".
    anchors = []

    for t, kind, price in candidates:
        if not anchors or t - anchors[-1][0] >= FAST_V13_WAVE_MS:
            anchors.append((t, kind, price))

    current_kind = classify_fast_v13(cur)

    result = {
        "fast_shadow": bool(current_kind),
        "fast_kind": current_kind,
        "fast_active": False,
        "fast_anchor_time": None,
        "fast_anchor_price": None,
        "fast_anchor_kind": None,
        "fast_elapsed_min": None,
        "fast_max_move": None,
        "fast_min_move": None,
        "confirm_30m": None,
        "confirm_45m": None,
        "confirmed_fast": False,
        "thin_watch": current_kind == "THIN",
        "fast_v13_version": "v13.1"
    }

    if not anchors:
        return result

    anchor_time, anchor_kind, anchor_price = anchors[-1]

    # No longer an active wave.
    if now_ms - anchor_time > FAST_V13_WAVE_MS:
        return result

    path = []

    for r in records:
        rt = int(r.get("time", 0))

        if anchor_time <= rt <= now_ms:
            price = _v13_float(r.get("price"))

            if price is not None and price > 0:
                path.append(price)

    if not path or anchor_price <= 0:
        return result

    max_move = (max(path) / anchor_price - 1) * 100
    min_move = (min(path) / anchor_price - 1) * 100
    elapsed = now_ms - anchor_time

    follow_ok = (
        min_move >= -3.0
        and max_move >= 1.0
    )

    confirm30 = (
        follow_ok
        if elapsed >= FAST_V13_CONFIRM30_MS
        else None
    )

    confirm45 = (
        follow_ok
        if elapsed >= FAST_V13_CONFIRM45_MS
        else None
    )

    confirmed = False

    if anchor_kind in ("FAST_BREAKOUT", "FAST_VOL"):
        confirmed = confirm30 is True

    elif anchor_kind == "FAST_ACC":
        confirmed = confirm45 is True

    # THIN stays experimental — never promote to confirmed_fast.
    elif anchor_kind == "THIN":
        confirmed = False

    result.update({
        "fast_active": True,
        "fast_anchor_time": anchor_time,
        "fast_anchor_price": anchor_price,
        "fast_anchor_kind": anchor_kind,
        "fast_elapsed_min": round(elapsed / 60000, 1),
        "fast_max_move": round(max_move, 2),
        "fast_min_move": round(min_move, 2),
        "confirm_30m": confirm30,
        "confirm_45m": confirm45,
        "confirmed_fast": confirmed,
        "thin_watch": anchor_kind == "THIN",
    })

    return result



def save_dashboard_data(out, alerts_enabled=True):
    data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    # Keep dashboard history strictly IRT-only.
    data = {
        symbol: history
        for symbol, history in data.items()
        if str(symbol).endswith("IRT")
    }

    now = int(time.time() * 1000)

    for x in out:
        symbol = x["symbol"]

        sequence_score, sequence_flags = sequence_score_v0(data.get(symbol, []), x)
        sequence_score_v1_value, sequence_flags_v1 = sequence_score_v1(data.get(symbol, []), x)
        shadow_v12 = (x.get("status") == "PRE_EARLY" and sequence_score_v1_value < 20)
        shadow_v12_label = "LOW_RISK_PRESETUP" if shadow_v12 else None

        rh = data.get(symbol, [])
        prev = rh[-1] if rh else {}

        # Shadow V13: fingerprint + time-based follow-through.
        fast_v13 = evaluate_fast_v13(rh, x, now)

        # ===== WAKE FORWARD RESEARCH v1 =====
        # Forward-only research:
        # - first wake of each 12h wave
        # - follow-through allowed for 2h
        # - STRONG_WAKE threshold is frozen at F15/F1 >= 0.10

        WAKE_WAVE_MS = 12 * 60 * 60 * 1000
        WAKE_FOLLOW_MS = 2 * 60 * 60 * 1000

        cur_t15 = float(x.get("trades15") or 0)
        cur_t1h = float(x.get("trades1h") or 0)
        cur_t4h = float(x.get("trades4h") or 0)
        cur_price_wake = float(x.get("price") or 0)

        # A previous snapshot is valid for Wake acceleration only when recent.
        # This prevents stale snapshots after scan outages/restarts from creating
        # artificial A1/A4 acceleration.
        WAKE_PREV_MAX_AGE_MS = 60 * 60 * 1000
        prev_time_wake = int(prev.get("time") or 0)
        wake_prev_age_ms = (now - prev_time_wake) if prev_time_wake > 0 else None
        wake_prev_gap_min = (
            wake_prev_age_ms / 60000.0
            if wake_prev_age_ms is not None
            else None
        )
        wake_prev_valid = bool(
            wake_prev_age_ms is not None
            and 0 < wake_prev_age_ms <= WAKE_PREV_MAX_AGE_MS
        )

        # Price Direction Shadow - research/logging only.
        # Find the most recent non-zero price within the same 60m lookback.
        wake_prev_price = None
        wake_prev_price_gap_min = None
        wake_pre_price_move_pct = None

        for pr in reversed(rh):
            pr_time = int(pr.get("time") or 0)
            pr_price = float(pr.get("price") or 0)
            pr_age_ms = now - pr_time if pr_time > 0 else None

            if (
                pr_price > 0
                and pr_age_ms is not None
                and 0 < pr_age_ms <= WAKE_PREV_MAX_AGE_MS
            ):
                wake_prev_price = pr_price
                wake_prev_price_gap_min = pr_age_ms / 60000.0

                if cur_price_wake > 0:
                    wake_pre_price_move_pct = (
                        100.0 * (cur_price_wake / wake_prev_price - 1.0)
                    )
                break

        if wake_prev_valid:
            prev_t1h_wake = float(prev.get("trades1h") or 0)
            prev_t4h_wake = float(prev.get("trades4h") or 0)
            wake_a1 = cur_t1h - prev_t1h_wake
            wake_a4 = cur_t4h - prev_t4h_wake
        else:
            wake_a1 = None
            wake_a4 = None

        wake_short_raw = (
            wake_prev_valid
            and x.get("p15") is None
            and cur_price_wake > 0
            and cur_t15 >= 2
            and cur_t1h >= 6
            and cur_t4h >= 8
            and wake_a1 >= 2
            and wake_a4 >= 2
        )

        wake_deep_raw = (
            wake_prev_valid
            and x.get("p15") is None
            and cur_price_wake > 0
            and cur_t4h >= 20
            and wake_a4 >= 8
            and (wake_a4 / max(cur_t4h, 1)) >= 0.30
        )

        # P15-Wake Shadow - research/logging only.
        # Tests the same raw Wake activity fingerprint when p15 exists.
        # NO effect on Wake, Hunt, status, alerts, or trading logic.
        p15_wake_shadow_short = bool(
            wake_prev_valid
            and x.get("p15") is not None
            and cur_price_wake > 0
            and cur_t15 >= 2
            and cur_t1h >= 6
            and cur_t4h >= 8
            and wake_a1 >= 2
            and wake_a4 >= 2
        )

        p15_wake_shadow_deep = bool(
            wake_prev_valid
            and x.get("p15") is not None
            and cur_price_wake > 0
            and cur_t4h >= 20
            and wake_a4 >= 8
            and (wake_a4 / max(cur_t4h, 1)) >= 0.30
        )

        p15_wake_shadow_raw = bool(
            p15_wake_shadow_short or p15_wake_shadow_deep
        )

        p15_wake_shadow_dt15 = None
        p15_wake_shadow_dt1 = None
        p15_wake_shadow_da1 = None

        if wake_prev_valid:
            prev_t15_shadow = float(prev.get("trades15") or 0)
            p15_wake_shadow_dt15 = cur_t15 - prev_t15_shadow
            p15_wake_shadow_dt1 = cur_t1h - prev_t1h_wake

            prev_wake_a1_shadow = prev.get("wake_a1")
            if prev_wake_a1_shadow is not None and wake_a1 is not None:
                p15_wake_shadow_da1 = (
                    wake_a1 - float(prev_wake_a1_shadow)
                )

        recent_short = any(
            bool(r.get("wake_short"))
            and 0 < now - int(r.get("time", 0)) < WAKE_WAVE_MS
            for r in rh
        )

        recent_deep = any(
            bool(r.get("wake_deep"))
            and 0 < now - int(r.get("time", 0)) < WAKE_WAVE_MS
            for r in rh
        )

        wake_short = bool(wake_short_raw and not recent_short)
        wake_deep = bool(wake_deep_raw and not recent_deep)

        wake_fresh_ratio = (
            cur_t15 / max(cur_t1h, 1)
            if cur_t1h > 0
            else 0.0
        )

        wake_follow = False
        strong_wake = False

        wake_anchor_time = None
        wake_anchor_price = None
        wake_anchor_kind = None
        wake_anchor_age_min = None
        wake_anchor_price_move = None

        # Trade Momentum v1 - research only, no effect on Strong Wake
        trade_momentum_e15_e1 = None
        trade_momentum_e1_e4 = None
        trade_momentum_g15 = None
        trade_momentum_g1 = None
        trade_momentum_g4 = None

        # Only previously recorded forward Wake rows can become anchors.
        for wr in reversed(rh):
            wt = int(wr.get("time", 0) or 0)

            if wt <= 0:
                continue

            age = now - wt

            if age <= 0:
                continue

            if age > WAKE_FOLLOW_MS:
                break

            if not (wr.get("wake_short") or wr.get("wake_deep")):
                continue

            wp = float(wr.get("price") or 0)
            wt15 = float(wr.get("trades15") or 0)
            wt1h = float(wr.get("trades1h") or 0)
            wt4h = float(wr.get("trades4h") or 0)

            price_move = None
            price_ok = True
            if wp > 0 and cur_price_wake > 0:
                price_move = 100.0 * (cur_price_wake / wp - 1.0)
                price_ok = price_move >= -3.0

            activity_ok = (
                cur_t4h > wt4h
                and (cur_t1h >= wt1h or cur_t15 >= wt15)
            )

            if activity_ok and price_ok:
                wake_follow = True

                wake_anchor_time = wt
                wake_anchor_price = wp if wp > 0 else None
                wake_anchor_kind = (
                    "DEEP"
                    if bool(wr.get("wake_deep"))
                    else "SHORT"
                )
                wake_anchor_age_min = round(age / 60000.0, 1)
                wake_anchor_price_move = (
                    round(price_move, 4)
                    if price_move is not None
                    else None
                )

                # Trade Momentum v1 - frozen research features
                trade_momentum_e15_e1 = round(
                    cur_t15 / max(cur_t1h, 1), 4
                )
                trade_momentum_e1_e4 = round(
                    cur_t1h / max(cur_t4h, 1), 4
                )
                trade_momentum_g15 = round(
                    cur_t15 / max(wt15, 1), 4
                )
                trade_momentum_g1 = round(
                    cur_t1h / max(wt1h, 1), 4
                )
                trade_momentum_g4 = round(
                    cur_t4h / max(wt4h, 1), 4
                )

                if (
                    bool(wr.get("wake_deep"))
                    and wake_fresh_ratio >= 0.10
                ):
                    strong_wake = True

                break

        cp15 = x.get("p15")
        pp15 = prev.get("p15")
        cva = float(x.get("va") or 0)
        ct15 = int(x.get("trades15") or 0)
        prev_t15 = [int(r.get("trades15") or 0) for r in rh[-3:]]
        pump_accum_v1 = (x.get("status") == "WATCH_ACCUMULATION" and cp15 is not None and float(cp15) <= 0 and cva >= 3)
        pump_recovery_v1 = (pump_accum_v1 and pp15 is not None and -3 <= float(cp15) <= 0 and float(cp15)-float(pp15) >= 1)
        dead_wakeup_v1 = (ct15 >= 8 and bool(prev_t15) and max(prev_t15) <= 4)
        pump_combo_oos_v1 = (pump_accum_v1 and cp15 is not None and float(cp15) <= -1.5 and ct15 >= 15 and sequence_score_v1_value < 25)

        # PRE-WAKE V1 - research only.
        # Historical candidate fingerprint; does NOT affect status/Hunt/alerts.
        pre_wake_v1 = (
            x.get("status") == "WATCH_ACCUMULATION"
            and cp15 is not None
            and -2.0 <= float(cp15) <= 0.0
            and sequence_score_v1_value >= 30
            and float(x.get("va") or 0) >= 2.0
            and float(x.get("bs") or 0) >= 1.5
        )
        powr_bs_oos_v1 = (x.get("status") == "SCANNED" and cp15 is not None and -1 <= float(cp15) <= 2 and float(x.get("vr") or 0) >= 5 and cva >= 5 and sequence_score_v1_value < 25 and not bool(x.get("breakout")) and float(x.get("bs") or 0) >= 1)

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
            "trades4h": x.get("trades4h", 0),
            "structure1h": x.get("structure1h"),
            "structure4h": x.get("structure4h"),
            "compression15": x.get("compression15"),
            "sequence_score": sequence_score,
            "sequence_flags": sequence_flags,
            "sequence_version": "v0",
            "sequence_score_v1": sequence_score_v1_value,
            "sequence_flags_v1": sequence_flags_v1,
            "sequence_version_v1": "v1.1",
            "shadow_v12": shadow_v12,
            "shadow_v12_label": shadow_v12_label,

            # Shadow V13 research fields.
            "fast_shadow": fast_v13["fast_shadow"],
            "fast_kind": fast_v13["fast_kind"],
            "fast_active": fast_v13["fast_active"],
            "fast_anchor_time": fast_v13["fast_anchor_time"],
            "fast_anchor_price": fast_v13["fast_anchor_price"],
            "fast_anchor_kind": fast_v13["fast_anchor_kind"],
            "fast_elapsed_min": fast_v13["fast_elapsed_min"],
            "fast_max_move": fast_v13["fast_max_move"],
            "fast_min_move": fast_v13["fast_min_move"],
            "confirm_30m": fast_v13["confirm_30m"],
            "confirm_45m": fast_v13["confirm_45m"],
            "confirmed_fast": fast_v13["confirmed_fast"],
            "thin_watch": fast_v13["thin_watch"],
            "fast_v13_version": fast_v13["fast_v13_version"],

            # Wake forward-research fields.
            "wake_short": wake_short,
            "wake_deep": wake_deep,
            "wake_follow": wake_follow,
            "wake_fresh_ratio": round(wake_fresh_ratio, 4),

            # Wake acceleration / previous-snapshot validity.
            "wake_a1": wake_a1,
            "wake_a4": wake_a4,
            "wake_prev_gap_min": (
                round(wake_prev_gap_min, 4)
                if wake_prev_gap_min is not None
                else None
            ),
            "wake_prev_valid": wake_prev_valid,

            # P15-Wake Shadow - research/logging only.
            # No effect on Wake, Hunt, status, alerts, or trading logic.
            "p15_wake_shadow_raw": p15_wake_shadow_raw,
            "p15_wake_shadow_short": p15_wake_shadow_short,
            "p15_wake_shadow_deep": p15_wake_shadow_deep,
            "p15_wake_shadow_da1": (
                round(p15_wake_shadow_da1, 4)
                if p15_wake_shadow_da1 is not None
                else None
            ),
            "p15_wake_shadow_dt15": (
                round(p15_wake_shadow_dt15, 4)
                if p15_wake_shadow_dt15 is not None
                else None
            ),
            "p15_wake_shadow_dt1": (
                round(p15_wake_shadow_dt1, 4)
                if p15_wake_shadow_dt1 is not None
                else None
            ),

            # Price Direction Shadow - research/logging only.
            "wake_prev_price": wake_prev_price,
            "wake_prev_price_gap_min": (
                round(wake_prev_price_gap_min, 4)
                if wake_prev_price_gap_min is not None
                else None
            ),
            "wake_pre_price_move_pct": (
                round(wake_pre_price_move_pct, 4)
                if wake_pre_price_move_pct is not None
                else None
            ),

            "strong_wake": strong_wake,
            "wake_anchor_time": wake_anchor_time,
            "wake_anchor_price": wake_anchor_price,
            "wake_anchor_kind": wake_anchor_kind,
            "wake_anchor_age_min": wake_anchor_age_min,
            "wake_anchor_price_move": wake_anchor_price_move,

            # Trade Momentum v1 - research only
            "trade_momentum_e15_e1": trade_momentum_e15_e1,
            "trade_momentum_e1_e4": trade_momentum_e1_e4,
            "trade_momentum_g15": trade_momentum_g15,
            "trade_momentum_g1": trade_momentum_g1,
            "trade_momentum_g4": trade_momentum_g4,

            "research_version_v1": "v1",
            "pre_wake_v1": pre_wake_v1,
            "pre_wake_version": "v1",
            "pump_accum_v1": pump_accum_v1,
            "pump_recovery_v1": pump_recovery_v1,
            "dead_wakeup_v1": dead_wakeup_v1,
            "pump_combo_oos_v1": pump_combo_oos_v1,
            "powr_bs_oos_v1": powr_bs_oos_v1
        }

        history = data.get(symbol, [])
        history.append(row)
        data[symbol] = history[-100:]

    tmp = DATA_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    os.replace(tmp, DATA_FILE)

    # Persist Strong Wake forward events independently of the 100-row history cap.
    update_strong_wake_ledger(data, now)

    # PRE-WAKE V1 forward research only; no Hunt/status/phone-alert effect.
    update_pre_wake_ledger(data, now)

    # HUNT QUIET-WAKE V1 - prospective research ledger; NO phone alerts.
    update_quiet_wake(data, now)
    update_quiet_wake_outcomes(data, now)
    update_quiet_wake_snapshots(data, now)

    # BREAKOUT OBSERVER V1 - research only; no alerts.
    update_breakout_observer(data, now)

    # MOVE TRACKER V1 LIVE.
    # Persist waves/milestones and send milestone phone alerts.
    update_move_tracker(data, now, alerts_enabled=alerts_enabled)

    live_file = os.path.join(os.path.dirname(__file__), "tabdeal_radar_live.json")
    live_data = {symbol: history[-5:] for symbol, history in data.items() if history and symbol not in {"XRDIRT","BTCIRT","ETHIRT","USDTIRT","TRXIRT","XRPIRT","SOLIRT","ADAIRT","BNBIRT"}}
    live_tmp = live_file + ".tmp"
    with open(live_tmp, "w", encoding="utf-8") as f:
        json.dump(live_data, f, ensure_ascii=False)
    os.replace(live_tmp, live_file)


def safe_score(m):
    try:
        return True, score(m), None
    except Exception as e:
        return False, None, e


def run_once(max_markets=0, alerts_enabled=True, entry_v1_shadow=False, move_early_v2_shadow=False):
    markets=get_markets()
    if max_markets: markets=markets[:max_markets]

    persistence = load_persistence()

    out=[]

    scan_ok = 0
    scan_errors = 0

    # Parallelize only network-heavy score() work.
    # executor.map preserves the exact original market order.
    with ThreadPoolExecutor(max_workers=4) as executor:
        score_results = list(executor.map(safe_score, markets))

    for m, result in zip(markets, score_results):
        try:
            ok, s, score_error = result
            if not ok:
                raise score_error

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

                    # Persistence is useful only when real power confirms it.
                    # Do not reward persistence alone.
                    vr_now = float(s.get("vr") or 0)
                    breakout_now = bool(s.get("breakout", False))

                    strength_confirmed = (
                        breakout_now
                        or vr_now >= 0.8
                    )

                    if (
                        status in ("PRE_EARLY", "EARLY")
                        and s.get("pre_score") is not None
                        and float(s.get("pre_score") or 0) >= 40
                        and strength_confirmed
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

                        # Weak volume + no breakout must never become a strong leader.
                        if (
                            not s.get("breakout", False)
                            and float(s.get("vr") or 0) < 0.5
                        ):
                            hunt = min(hunt, 59.0)

                        # A breakout without real power confirmation is fragile.
                        # Do not allow breakout alone to create a strong leader.
                        if (
                            s.get("breakout", False)
                            and float(s.get("vr") or 0) < 0.5
                            and float(s.get("va") or 0) < 1.0
                            and float(s.get("bs") or 0) < 1.0
                        ):
                            hunt = min(hunt, 55.0)

                        # First EARLY hit is only an alert, not a confirmed leader.
                        # Require at least 2 consecutive candidate scans for full Hunt score.
                        if (
                            status == "EARLY"
                            and int(s.get("persistence", 0) or 0) < 2
                        ):
                            hunt = min(hunt, 78.0)

                        ignition_bonus = (status == "EARLY" and 0 <= float(s.get("p15") or 0) <= 1 and float(s.get("va") or 0) >= 5 and float(s.get("bs") or 0) >= 2 and bool(s.get("breakout")) and float(s.get("book") or 0) >= 1 and float(s.get("sequence_score_v1") or 0) >= 40)
                        s["ignition_bonus"] = bool(ignition_bonus)
                        if ignition_bonus:
                            hunt += 5.0

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
    save_dashboard_data(out, alerts_enabled=alerts_enabled)
    save_ignition_history(out, IGNITION_HISTORY_FILE)

    # MOVE-EARLY V2 SHADOW — Termux prospective research only.
    if move_early_v2_shadow:
        try:
            run_move_early_v2_shadow()
        except Exception as e:
            print(f"MOVE_EARLY_V2_CALL_ERROR: {type(e).__name__}: {e}")

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
                f"IGN={'Y' if s.get('ignition_bonus') else 'N'} "
                f"P={s.get('persistence',0)} "
                f"{s.get('status','?')}"
            )
    # HUNT ENTRY V1 SHADOW — Termux prospective cohort only.
    if entry_v1_shadow:
        try:
            with open("tabdeal_radar_v21_data.json", "r") as f:
                v1_history = json.load(f)
            update_entry_v1_outcomes(v1_history)
        except Exception as e:
            print(f"HUNT_V1_OUTCOME_ERROR: {type(e).__name__}: {e}")

    if candidates:
        if entry_v1_shadow:
            # Frozen prospective research only; NO phone alert.
            register_entry_v1(candidates)

        # انتخاب اصلی = قوی‌ترین Hunt Score واقعی.
        # EARLY و PRE_EARLY هر دو در یک رتبه‌بندی قرار دارند.
        best = candidates[0]

        print(
            "\n🥇 انتخاب اصلی:",
            best["symbol"],
            f"Hunt={best['hunt_score']:.1f}/100",
            f"Status={best['status']}"
        )

        if alerts_enabled and best.get("hunt_score", 0) >= 80 and best.get("status") in ("EARLY", "PRE_EARLY"):
            save_alert_history(best)

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
    p.add_argument("--no-alerts",action="store_true")
    p.add_argument("--entry-v1-shadow",action="store_true")
    p.add_argument("--move-early-v2-shadow",action="store_true")
    a=p.parse_args()
    if a.once:
        run_once(a.max_markets, alerts_enabled=not a.no_alerts, entry_v1_shadow=a.entry_v1_shadow, move_early_v2_shadow=a.move_early_v2_shadow); return
    while True:
        try: run_once(a.max_markets, alerts_enabled=not a.no_alerts, entry_v1_shadow=a.entry_v1_shadow, move_early_v2_shadow=a.move_early_v2_shadow)
        except Exception as e: print("RADAR ERROR:",e)
        time.sleep(max(15,a.interval))

if __name__=="__main__":
    main()
