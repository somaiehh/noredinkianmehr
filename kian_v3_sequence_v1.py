import math

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
