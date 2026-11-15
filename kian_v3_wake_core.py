
import copy

_LEDGER = {"strong": {}, "pre": {}}

def _load_strong():
    return copy.deepcopy(_LEDGER["strong"])

def _save_strong(data):
    _LEDGER["strong"] = copy.deepcopy(data)

def _load_pre():
    return copy.deepcopy(_LEDGER["pre"])

def _save_pre(data):
    _LEDGER["pre"] = copy.deepcopy(data)
def update_strong_wake_ledger(data, now_ms):
    events = _load_strong()

    def find_existing_strong_event(symbol, event_time, anchor_time):
        for e in events:
            if e.get('symbol') != symbol:
                continue
            old_anchor = e.get('anchor_time')
            if anchor_time is not None and old_anchor is not None:
                try:
                    if int(anchor_time) == int(old_anchor):
                        return e
                except Exception:
                    pass
                continue
            old_time = int(e.get('event_time', 0) or 0)
            if old_time > 0 and abs(event_time - old_time) <= 2 * 60 * 60 * 1000:
                return e
        return None
    for symbol, history in data.items():
        if not isinstance(history, list):
            continue
        for r in history:
            if not r.get('strong_wake'):
                continue
            event_time = int(r.get('time', 0) or 0)
            event_price = float(r.get('price') or 0)
            if event_time <= 0 or event_price <= 0:
                continue
            anchor_time = r.get('wake_anchor_time')
            existing_event = find_existing_strong_event(symbol, event_time, anchor_time)
            if existing_event is not None:
                if existing_event.get('anchor_time') is None and anchor_time is not None:
                    existing_event['anchor_time'] = anchor_time
                    existing_event['anchor_price'] = r.get('wake_anchor_price')
                    existing_event['anchor_kind'] = r.get('wake_anchor_kind')
                    existing_event['legacy'] = False
                continue
            events.append({'symbol': symbol, 'event_time': event_time, 'event_price': event_price, 'anchor_time': r.get('wake_anchor_time'), 'anchor_price': r.get('wake_anchor_price'), 'anchor_kind': r.get('wake_anchor_kind'), 'fresh_ratio': r.get('wake_fresh_ratio'), 'tm_e15_e1': r.get('trade_momentum_e15_e1'), 'tm_e1_e4': r.get('trade_momentum_e1_e4'), 'tm_g15': r.get('trade_momentum_g15'), 'tm_g1': r.get('trade_momentum_g1'), 'tm_g4': r.get('trade_momentum_g4'), 'legacy': r.get('wake_anchor_time') is None, 'result': 'PENDING', 'result_time': None, 'result_move': None, 'max_move': 0.0, 'min_move': 0.0, 'ledger_version': 'wake-ledger-v1'})
    for e in events:
        if e.get('result') == 'EXPIRED_NONE':
            continue
        symbol = e.get('symbol')
        t0 = int(e.get('event_time', 0) or 0)
        p0 = float(e.get('event_price') or 0)
        if t0 <= 0 or p0 <= 0:
            continue
        history = data.get(symbol, [])
        max_move = e.get('max_move')
        min_move = e.get('min_move')
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
            t = int(r.get('time', 0) or 0)
            if t <= t0 or t > t0 + STRONG_WAKE_HORIZON_MS:
                continue
            price = float(r.get('price') or 0)
            if price <= 0:
                continue
            candidates.append((t, price))
        candidates.sort()
        for t, price in candidates:
            move = 100.0 * (price / p0 - 1.0)
            max_move = max(max_move, move)
            min_move = min(min_move, move)
            if move >= STRONG_WAKE_HIT_PCT:
                e['result'] = 'HIT_FIRST'
                e['result_time'] = t
                e['result_move'] = round(move, 4)
                break
            if move <= STRONG_WAKE_STOP_PCT:
                e['result'] = 'STOP_FIRST'
                e['result_time'] = t
                e['result_move'] = round(move, 4)
                break
        e['max_move'] = round(max_move, 4)
        e['min_move'] = round(min_move, 4)
        if e.get('result') == 'PENDING' and now_ms >= t0 + STRONG_WAKE_HORIZON_MS:
            e['result'] = 'EXPIRED_NONE'
    events.sort(key=lambda e: (int(e.get('event_time', 0)), str(e.get('symbol', ''))))
    _save_strong(events)
    return events

def update_pre_wake_ledger(data, now_ms):
    events = _load_pre()
    existing = {(str(e.get('symbol', '')), int(e.get('event_time', 0) or 0)) for e in events}
    for symbol, history in data.items():
        if not isinstance(history, list):
            continue
        for r in history:
            if not r.get('pre_wake_v1'):
                continue
            event_time = int(r.get('time', 0) or 0)
            event_price = float(r.get('price') or 0)
            if event_time <= 0 or event_price <= 0:
                continue
            key = (symbol, event_time)
            if key in existing:
                continue
            events.append({'symbol': symbol, 'event_time': event_time, 'event_price': event_price, 'status': r.get('status'), 'sequence_score_v1': r.get('sequence_score_v1'), 'p15': r.get('p15'), 'vr': r.get('vr'), 'va': r.get('va'), 'ta': r.get('ta'), 'bs': r.get('bs'), 'book': r.get('book'), 'breakout': r.get('breakout'), 'fast_v13': r.get('fast_v13'), 'result': 'PENDING', 'result_time': None, 'result_move': None, 'max_move': 0.0, 'min_move': 0.0, 'ledger_version': 'pre-wake-ledger-v1'})
            existing.add(key)
    for e in events:
        if e.get('result') != 'PENDING':
            continue
        symbol = e.get('symbol')
        t0 = int(e.get('event_time', 0) or 0)
        p0 = float(e.get('event_price') or 0)
        if t0 <= 0 or p0 <= 0:
            continue
        history = data.get(symbol, [])
        try:
            max_move = float(e.get('max_move', 0) or 0)
        except Exception:
            max_move = 0.0
        try:
            min_move = float(e.get('min_move', 0) or 0)
        except Exception:
            min_move = 0.0
        candidates = []
        for r in history:
            t = int(r.get('time', 0) or 0)
            if t <= t0 or t > t0 + PRE_WAKE_HORIZON_MS:
                continue
            price = float(r.get('price') or 0)
            if price <= 0:
                continue
            candidates.append((t, price))
        candidates.sort()
        first_result = e.get('result') != 'PENDING'
        for t, price in candidates:
            move = 100.0 * (price / p0 - 1.0)
            max_move = max(max_move, move)
            min_move = min(min_move, move)
            if not first_result:
                if move >= PRE_WAKE_HIT_PCT:
                    e['result'] = 'HIT_FIRST'
                    e['result_time'] = t
                    e['result_move'] = round(move, 4)
                    first_result = True
                elif move <= PRE_WAKE_STOP_PCT:
                    e['result'] = 'STOP_FIRST'
                    e['result_time'] = t
                    e['result_move'] = round(move, 4)
                    first_result = True
        e['max_move'] = round(max_move, 4)
        e['min_move'] = round(min_move, 4)
        if e.get('result') == 'PENDING' and now_ms >= t0 + PRE_WAKE_HORIZON_MS:
            e['result'] = 'EXPIRED_NONE'
    events.sort(key=lambda e: (int(e.get('event_time', 0)), str(e.get('symbol', ''))))
    _save_pre(events)
    return events
