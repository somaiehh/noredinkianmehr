import json
import math
import fcntl
from pathlib import Path


def load_wave_context(state_dir, allowed_symbols):
    root = Path(state_dir).expanduser().resolve()
    safe = (Path.home() / "kian_v3_upgrade_lab").resolve()

    if safe not in root.parents:
        raise ValueError("UNSAFE_STATE_PATH")

    path = root / "tracker.json"
    lock_path = root / "tracker.lock"

    if not path.exists():
        return {}

    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    if data.get("version") != 4:
        raise ValueError("INVALID_TRACKER_VERSION")

    if not isinstance(data.get("waves"), dict):
        raise ValueError("INVALID_WAVES")

    result = {}

    for wave_id, wave in data["waves"].items():
        if not isinstance(wave_id, str) or not isinstance(wave, dict):
            continue

        symbol, sep, anchor = wave_id.rpartition(":")

        if (
            not sep
            or symbol not in allowed_symbols
            or not anchor.isdigit()
        ):
            continue

        prices = (
            wave.get("anchor_price"),
            wave.get("peak_price"),
            wave.get("last_price"),
        )

        if not all(
            type(p) in (int, float)
            and math.isfinite(p)
            and p > 0
            for p in prices
        ):
            continue

        anchor_price, peak_price, last_price = prices

        if peak_price < max(anchor_price, last_price):
            continue

        evidence = wave.get("migration_evidence") or {}
        correction = wave.get("open_correction")
        reignitions = wave.get("reignitions") or []

        valid_reignition_times = sorted(
            event["confirmation_time"]
            for event in reignitions
            if isinstance(event, dict)
            and type(event.get("confirmation_time")) is int
            and int(anchor) <= event["confirmation_time"]
            and event["confirmation_time"] <= wave.get("last_time", 0)
        )

        context = {
            "wave_id": wave_id,
            "last_reignition_time": (
                valid_reignition_times[-1]
                if valid_reignition_times else None
            ),
            "verified_reignition_count": len(
                valid_reignition_times
            ),
            "symbol": symbol,
            "anchor_time": int(anchor),
            "anchor_price": anchor_price,
            "peak_price": peak_price,
            "last_price": last_price,
            "last_time": wave.get("last_time"),
            "drawdown_from_peak_pct": round(
                100 * (last_price / peak_price - 1), 4
            ),
            "correction_status": (
                "TRACKED_CORRECTION"
                if isinstance(correction, dict)
                else "HISTORICAL_CORRECTION_UNVERIFIED"
                if evidence.get("correction_start_verified") is False
                else "NO_OPEN_CORRECTION"
            ),
            "reignition_count": len(reignitions),
            "historical_low_verified": evidence.get(
                "historical_low_verified"
            ),
            "monitoring": "MONITOR_ONLY",
            "approved_for_trade": False,
        }

        result.setdefault(symbol, []).append(context)

    for symbol in result:
        result[symbol].sort(
            key=lambda w: (w["last_time"] or 0, w["wave_id"]),
            reverse=True,
        )

    return result
