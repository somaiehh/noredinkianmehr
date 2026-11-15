"""Persistent preliminary watch for unverified V3 candidates."""

import fcntl
import json
import math
import os
import tempfile
from pathlib import Path



DRAWDOWN_TRIGGER = 5.0

def _update_correction_history(wave, timestamp, price):
    """Record observed corrections without authorizing trading."""
    wave.setdefault("open_correction", None)
    wave.setdefault("reignitions", [])

    correction = wave["open_correction"]
    previous_peak = float(wave["peak_price"])

    if correction is None:
        if price > previous_peak:
            wave["peak_price"] = price
            previous_peak = price

        drawdown = 100.0 * (price / previous_peak - 1.0)

        if drawdown <= -DRAWDOWN_TRIGGER:
            wave["open_correction"] = {
                "start_time": timestamp,
                "prior_peak": previous_peak,
                "low": price,
                "low_time": timestamp,
            }
    else:
        if price < correction["low"]:
            correction["low"] = price
            correction["low_time"] = timestamp

        if price > correction["prior_peak"]:
            wave["reignitions"].append({
                "episode_time": correction["start_time"],
                "confirmation_time": timestamp,
                "prior_peak": correction["prior_peak"],
                "correction_low": correction["low"],
                "breakout_price": price,
                "evidence_status": "PRICE_ONLY_UNVERIFIED",
                "notification_sent": False,
            })
            wave["open_correction"] = None
            wave["peak_price"] = price

    wave["fast_evidence_status"] = "FAST_UNVERIFIED"
    wave["approved_for_trade"] = False
    wave["real_alert_sent"] = False


def observe_early_watch(path, symbol, anchor_time, scan_time,
                        price, anchor_price=None):
    if not isinstance(symbol, str) or not symbol.endswith("IRT"):
        raise ValueError("IRT_SYMBOL_REQUIRED")
    if (type(anchor_time) is not int or anchor_time <= 0
            or type(scan_time) is not int or scan_time < anchor_time):
        raise ValueError("INVALID_TIME")
    if (type(price) not in (int, float)
            or not math.isfinite(price) or price <= 0):
        raise ValueError("INVALID_PRICE")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    wave_id = f"{symbol}:{anchor_time}"

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        data = (
            json.loads(path.read_text())
            if path.exists()
            else {"version": 1, "waves": {}}
        )
        if (not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("waves"), dict)):
            raise ValueError("INVALID_EARLY_WATCH_STATE")

        waves = data["waves"]
        wave = waves.get(wave_id)

        if wave is None:
            known_anchor = (
                type(anchor_price) in (int, float)
                and math.isfinite(anchor_price)
                and anchor_price > 0
            )
            wave = {
                "symbol": symbol,
                "anchor_time": anchor_time,
                "anchor_price": float(anchor_price) if known_anchor else None,
                "first_observed_time": scan_time,
                "first_observed_price": float(price),
                "last_time": scan_time,
                "last_price": float(price),
                "peak_price": max(float(anchor_price), float(price))
                              if known_anchor else float(price),
                "peak_basis": "ANCHOR_AND_OBSERVED"
                              if known_anchor else "OBSERVED_ONLY",
                "fast_evidence_status": "FAST_UNVERIFIED",
                "approved_for_trade": False,
                "real_alert_sent": False,
            }
            waves[wave_id] = wave
        else:
            if scan_time < wave["last_time"]:
                return "STALE_REJECTED"
            if scan_time == wave["last_time"]:
                if float(price) != wave["last_price"]:
                    raise ValueError("SAME_TIME_PRICE_CONFLICT")
                return "DUPLICATE"

            wave["last_time"] = scan_time
            wave["last_price"] = float(price)
            _update_correction_history(wave, scan_time, float(price))

        fd, temp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".early_watch_"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

        return "EARLY_WATCH_SAVED"



def resume_early_watch_from_market_rows(
    path, scan_time, market_rows, allowed_irt_symbols
):
    """Atomically resume saved early waves from observed market prices."""
    if type(scan_time) is not int or scan_time <= 0:
        raise ValueError("INVALID_SCAN_TIME")
    if not isinstance(market_rows, list):
        raise ValueError("INVALID_MARKET_ROWS")
    if not isinstance(allowed_irt_symbols, (set, frozenset)):
        raise ValueError("INVALID_MARKET_UNIVERSE")

    observations = {}
    for row in market_rows:
        if not isinstance(row, dict):
            raise ValueError("INVALID_MARKET_ROW")
        symbol = row.get("symbol")
        timestamp = row.get("time")
        price = row.get("price")
        if (
            not isinstance(symbol, str)
            or type(timestamp) is not int
            or timestamp <= 0
            or timestamp > scan_time
            or type(price) not in (int, float)
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("INVALID_MARKET_OBSERVATION")
        key = (symbol, timestamp)
        value = float(price)
        if key in observations and observations[key] != value:
            raise ValueError("MARKET_PRICE_CONFLICT")
        observations[key] = value

    result = {
        "matched": 0,
        "updated": 0,
        "status": "MONITOR_ONLY",
        "real_alert_sent": False,
    }

    path = Path(path)
    if not path.exists():
        return result

    lock_path = Path(str(path) + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not path.exists():
            return result

        data = json.loads(path.read_text())
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("waves"), dict)
        ):
            raise ValueError("INVALID_EARLY_WATCH_STATE")

        changed = False
        for (symbol, timestamp), price in sorted(
            observations.items(), key=lambda x: (x[0][1], x[0][0])
        ):
            if symbol not in allowed_irt_symbols:
                continue

            for wave in data["waves"].values():
                if wave.get("symbol") != symbol:
                    continue
                if timestamp <= wave["last_time"]:
                    continue
                if timestamp < wave["anchor_time"]:
                    continue

                result["matched"] += 1
                wave["last_time"] = timestamp
                wave["last_price"] = price
                _update_correction_history(wave, timestamp, price)
                result["updated"] += 1
                changed = True

        if changed:
            fd, temp = tempfile.mkstemp(
                dir=str(path.parent), prefix=".early_watch_"
            )
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(data, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)

    return result


def record_observed_fast(path, symbol, anchor_time, observations):
    """Atomically attach unverified FAST observations to an existing wave."""
    if not isinstance(symbol, str) or not symbol.endswith("IRT"):
        raise ValueError("IRT_SYMBOL_REQUIRED")
    if type(anchor_time) is not int or anchor_time <= 0:
        raise ValueError("INVALID_ANCHOR")
    if not isinstance(observations, list):
        raise ValueError("INVALID_OBSERVATIONS")

    clean = []
    for row in observations:
        if not isinstance(row, dict):
            raise ValueError("INVALID_FAST_ROW")
        if row.get("symbol") != symbol or row.get("anchor_time") != anchor_time:
            raise ValueError("FAST_WAVE_MISMATCH")
        if row.get("coverage_verified") is not False:
            raise ValueError("UNSAFE_FAST_COVERAGE")
        if row.get("evidence_status") != "FAST_OBSERVED":
            raise ValueError("UNSAFE_FAST_STATUS")
        if row.get("approved_for_trade") is not False or row.get("real_alert_sent") is not False:
            raise ValueError("UNSAFE_FAST_FLAGS")

        timestamp = row.get("time")
        price = row.get("price")
        if (type(timestamp) is not int or timestamp < anchor_time
                or type(price) not in (int, float)
                or not math.isfinite(price) or price <= 0):
            raise ValueError("INVALID_FAST_TIME_OR_PRICE")

        windows = {}
        for name in ("1m", "3m", "5m"):
            item = row.get(name)
            if not isinstance(item, dict):
                raise ValueError("INVALID_FAST_WINDOW")
            count = item.get("trades")
            volume = item.get("volume")
            if (type(count) is not int or count < 0
                    or type(volume) not in (int, float)
                    or not math.isfinite(volume) or volume < 0):
                raise ValueError("INVALID_FAST_METRICS")
            windows[name] = {
                "trades": count,
                "observed_volume": float(volume),
            }

        clean.append({
            "time": timestamp,
            "price": float(price),
            "windows": windows,
        })

    if not clean:
        return "NO_OBSERVATIONS"

    latest = max(clean, key=lambda x: x["time"])
    path = Path(path)
    if not path.exists():
        return "WAVE_NOT_FOUND"

    lock_path = Path(str(path) + ".lock")
    wave_id = f"{symbol}:{anchor_time}"

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        if not path.exists():
            return "WAVE_NOT_FOUND"

        data = json.loads(path.read_text())
        if (not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("waves"), dict)):
            raise ValueError("INVALID_EARLY_WATCH_STATE")

        wave = data["waves"].get(wave_id)
        if wave is None:
            return "WAVE_NOT_FOUND"

        previous = wave.get("fast_observed")
        if isinstance(previous, dict):
            old_time = previous.get("time")
            if type(old_time) is int:
                if latest["time"] < old_time:
                    return "STALE_REJECTED"
                if latest["time"] == old_time:
                    if previous.get("observation") != latest:
                        raise ValueError("SAME_TIME_FAST_CONFLICT")
                    return "DUPLICATE"

        history = wave.get("fast_observed_history", [])
        if not isinstance(history, list):
            raise ValueError("INVALID_FAST_HISTORY")

        history = list(history)

        if not history and isinstance(previous, dict):
            prior = previous.get("observation")
            if isinstance(prior, dict):
                history.append(prior)

        by_time = {}
        for observation in history + [latest]:
            if not isinstance(observation, dict):
                raise ValueError("INVALID_FAST_HISTORY_ROW")
            timestamp = observation.get("time")
            if type(timestamp) is not int:
                raise ValueError("INVALID_FAST_HISTORY_TIME")
            if timestamp in by_time and by_time[timestamp] != observation:
                raise ValueError("FAST_HISTORY_CONFLICT")
            by_time[timestamp] = observation

        wave["fast_observed_history"] = [
            by_time[timestamp]
            for timestamp in sorted(by_time)[-48:]
        ]

        wave["fast_observed"] = {
            "time": latest["time"],
            "observation": latest,
            "evidence_status": "FAST_OBSERVED",
            "coverage_verified": False,
        }

        wave["fast_activity_assessment"] = (
            assess_observed_fast_activity(
                wave["fast_observed_history"]
            )
        )
        wave["fast_evidence_status"] = "FAST_UNVERIFIED"
        wave["approved_for_trade"] = False
        wave["real_alert_sent"] = False

        fd, temp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".early_watch_"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    return "FAST_OBSERVED_SAVED"


def assess_observed_fast_activity(history):
    """Assess observed activity without claiming verified market volume."""
    if not isinstance(history, list) or len(history) < 3:
        return {
            "status": "INSUFFICIENT_HISTORY",
            "approved_for_trade": False,
        }

    rows = history[-3:]
    times = [r.get("time") for r in rows if isinstance(r, dict)]

    if (
        len(times) != 3
        or any(type(t) is not int for t in times)
        or not (times[0] < times[1] < times[2])
    ):
        return {
            "status": "INVALID_HISTORY",
            "approved_for_trade": False,
        }

    counts = []
    volumes = []

    for row in rows:
        windows = row.get("windows")
        if not isinstance(windows, dict):
            return {
                "status": "INVALID_HISTORY",
                "approved_for_trade": False,
            }

        window = windows.get("1m")
        if not isinstance(window, dict):
            return {
                "status": "INVALID_HISTORY",
                "approved_for_trade": False,
            }

        count = window.get("trades")
        volume = window.get("observed_volume")

        if (
            type(count) is not int or count < 0
            or type(volume) not in (int, float)
            or not math.isfinite(volume) or volume < 0
        ):
            return {
                "status": "INVALID_HISTORY",
                "approved_for_trade": False,
            }

        counts.append(count)
        volumes.append(float(volume))

    trades_up = counts[0] < counts[1] < counts[2]
    volume_up = volumes[0] < volumes[1] < volumes[2]

    # Experimental two-sample acceleration, monitoring only.
    last_gap_ms = times[2] - times[1]
    acceleration_observed = (
        0 < last_gap_ms <= 1800000
        and counts[1] >= 1
        and volumes[1] > 0
        and counts[2] >= 3
        and counts[2] >= 2 * counts[1]
        and volumes[2] >= 2 * volumes[1]
    )

    # Experimental surge observation; overlapping windows remain unverified.
    span_ms = times[2] - times[0]
    surge_observed = (
        not (trades_up and volume_up)
        and 0 < span_ms <= 600000
        and counts[2] >= 3
        and counts[0] >= 1
        and volumes[0] > 0
        and counts[2] >= 2 * counts[0]
        and volumes[2] >= 2 * volumes[0]
    )

    return {
        "status": (
            "ACTIVITY_RISING_OBSERVED"
            if trades_up and volume_up
            else (
                "ACTIVITY_SURGE_OBSERVED"
                if surge_observed
                else (
                    "ACTIVITY_ACCELERATION_OBSERVED"
                    if acceleration_observed
                    else "NO_CONSISTENT_RISE"
                )
            )
        ),
        "trades_1m": counts,
        "observed_volume_1m": volumes,
        "trades_rising": trades_up,
        "volume_rising": volume_up,
        "coverage_verified": False,
        "approved_for_trade": False,
        "real_alert_sent": False,
    }
