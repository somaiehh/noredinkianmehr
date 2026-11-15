import json
import socket
import tempfile
import time
from pathlib import Path

def blocked(*args, **kwargs):
    raise RuntimeError("NETWORK_DISABLED_IN_V3_SMOKE")

socket.socket.connect = blocked
socket.create_connection = blocked

from kian_v3_integrated_lab_v1 import run_comprehensive_v3_scan

now = int(time.time() * 1000)
anchor = now - 600000

candidate = {
    "symbol": "TESTIRT",
    "anchor_time": anchor,
    "anchor_price": 2000.0,
    "price": 2040.0,
    "fast_observed": [
        {
            "symbol": "TESTIRT",
            "anchor_time": anchor,
            "time": now - 60000,
            "price": 2040.0,
            "evidence_status": "FAST_OBSERVED",
            "coverage_verified": False,
            "real_alert_sent": False,
        }
    ],
}

safe_parent = Path.home() / "kian_v3_upgrade_lab"
if not safe_parent.is_dir():
    raise RuntimeError("V3_LAB_ROOT_MISSING")

with tempfile.TemporaryDirectory(
    prefix="kian_v3_smoke_",
    dir=safe_parent,
) as temp:
    state_dir = Path(temp)

    args = dict(
        scan_time=now,
        allowed_irt_symbols=frozenset({"TESTIRT"}),
        lab_state_dir=state_dir,
        max_persistent_symbols=0,
        fetch_persistent_trades=False,
    )

    first = run_comprehensive_v3_scan(
        candidates=[candidate], **args
    )

    second = run_comprehensive_v3_scan(
        candidates=[], **args
    )

    print("FIRST:", json.dumps(first.get("scan_summary", {})))
    print("SECOND:", json.dumps(second.get("scan_summary", {})))

    assert first.get("status") == "MONITOR_ONLY"
    assert second.get("status") == "MONITOR_ONLY"
    assert first.get("real_alert_sent") is False
    assert second.get("real_alert_sent") is False

    assert first["scan_summary"]["persistent_waves"] >= 1
    assert second["scan_summary"]["persistent_only_waves"] >= 1

print("V3_GITHUB_SMOKE: PASS")
print("NO_REAL_ALERTS")
print("NO_NETWORK")
print("NO_PRODUCTION_CHANGES")
