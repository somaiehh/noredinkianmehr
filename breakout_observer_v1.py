import json, os

BREAKOUT_OBSERVER_FILE = "breakout_observer_state.json"
BREAKOUT_OBSERVER_WINDOW_MS = 12 * 60 * 60 * 1000

def load_breakout_observer():
    try:
        with open(BREAKOUT_OBSERVER_FILE,"r",encoding="utf-8") as f:
            x=json.load(f)
        return x if isinstance(x,dict) else {}
    except Exception:
        return {}

def save_breakout_observer(state):
    tmp=BREAKOUT_OBSERVER_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,BREAKOUT_OBSERVER_FILE)

BREAKOUT_OBSERVER_ARCHIVE_FILE = "breakout_observer_archive.json"

def load_breakout_observer_archive():
    try:
        with open(BREAKOUT_OBSERVER_ARCHIVE_FILE,"r",encoding="utf-8") as f:
            x=json.load(f)
        return x if isinstance(x,list) else []
    except Exception:
        return []

def save_breakout_observer_archive(archive):
    tmp=BREAKOUT_OBSERVER_ARCHIVE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(archive,f,ensure_ascii=False,indent=2)
    os.replace(tmp,BREAKOUT_OBSERVER_ARCHIVE_FILE)

def update_breakout_observer(data,now_ms):
    state=load_breakout_observer()
    archive=load_breakout_observer_archive()
    for symbol,history in data.items():
        if not isinstance(history,list) or not history: continue
        row=history[-1]
        t=int(row.get("time") or 0)
        p=float(row.get("price") or 0)
        if t<=0 or p<=0: continue
        wave=state.get(symbol)

        # Re-arm after 12h only when a fresh breakout is present.
        if wave is not None:
            old_t=int(wave.get("anchor_time") or 0)
            if old_t>0 and t-old_t>=BREAKOUT_OBSERVER_WINDOW_MS and row.get("breakout") and now_ms-t<=30*60*1000:
                key=(symbol,old_t)
                seen={(x.get("symbol"),int(x.get("anchor_time") or 0)) for x in archive}
                if key not in seen:
                    archive.append(dict(wave))
                wave=None
                state.pop(symbol,None)

        if wave is None:
            if not row.get("breakout"): continue
            if now_ms-t>30*60*1000: continue
            wave={
                "symbol":symbol,
                "anchor_time":t,
                "anchor_price":p,
                "status":"WATCHING",
                "created_time":now_ms,
                "last_time":t,
                "last_price":p,
                "max_gain_pct":0.0,
                "levels":[]
            }
            state[symbol]=wave

        anchor=float(wave.get("anchor_price") or 0)
        if anchor<=0: continue
        age=max(0,t-int(wave.get("anchor_time") or t))
        gain=100*(p/anchor-1)

        wave["last_time"]=t
        wave["last_price"]=p
        wave["max_gain_pct"]=round(max(float(wave.get("max_gain_pct") or 0),gain),4)

        if wave.get("status")=="WATCHING":
            if gain<=-3:
                wave["status"]="FAILED"
                wave["failed_time"]=t
                wave["failed_price"]=p
            elif gain>=3:
                wave["status"]="CONFIRMED_MOVE"
                wave["confirmed_time"]=t
                wave["confirmed_price"]=p

        if wave.get("status")=="CONFIRMED_MOVE":
            levels=set(int(x) for x in wave.get("levels",[]))
            for level in (5,10):
                if gain>=level and level not in levels:
                    levels.add(level)
                    wave.setdefault("events",[]).append({
                        "level":level,"time":t,"price":p,
                        "gain_pct":round(gain,4)
                    })
            wave["levels"]=sorted(levels)

        if age>=BREAKOUT_OBSERVER_WINDOW_MS and wave.get("status") in ("WATCHING","CONFIRMED_MOVE"):
            wave["status"]="EXPIRED"
            wave["expired_time"]=t

    save_breakout_observer(state)
    save_breakout_observer_archive(archive)
    return state
