import json, os, time

def save_ignition_history(out, path):
    try:
        history = json.load(open(path, "r", encoding="utf-8")) if os.path.exists(path) else []
        if not isinstance(history, list): history = []
        seen = {(str(r.get("symbol")), int(r.get("time") or 0)) for r in history}
        for x in out:
            if not x.get("ignition_bonus"): continue
            key = (str(x.get("symbol")), int(x.get("time") or 0))
            if key in seen: continue
            history.append({k:x.get(k) for k in ("symbol","time","price","hunt_score","p15","va","bs","book","sequence_score_v1","persistence","breakout","status")})
            seen.add(key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("IGN_HISTORY_ERROR:", type(e).__name__, e)
        return False
