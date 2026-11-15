"""CI-only synthetic run_once integration test."""

import json
import os
import socket
import tempfile
from contextlib import nullcontext
import time
from pathlib import Path
from unittest.mock import patch

assert os.environ.get("GITHUB_ACTIONS") == "true", "CI_ONLY"

def block_network(*args, **kwargs):
    raise AssertionError("NETWORK_ATTEMPT")

socket.socket.connect = block_network
socket.socket.connect_ex = block_network
socket.create_connection = block_network

import radar_live as radar
import kian_v3_migration_guard as migration
import kian_v3_integrated_lab_v1 as engine

def main():
    assert not os.environ.get("GITHUB_TOKEN")
    assert not os.environ.get("GH_TOKEN")

    lab = Path.home() / "kian_v3_upgrade_lab"
    lab.mkdir(exist_ok=True)

    with nullcontext(tempfile.mkdtemp(
        prefix="v3_runonce_final_", dir=lab
    )) as temp:
        root = Path(temp).resolve()
        state = root / "state"
        state.mkdir()

        # V3_FINAL_CI_AUDIT_GUARD_V1
        # Deny filesystem mutations outside this temporary test root.
        import sys

        def audit_guard(event, args):
            if event in (
                "subprocess.Popen",
                "os.system",
                "os.exec",
                "os.spawn",
                "os.posix_spawn",
                "os.fork",
            ):
                raise AssertionError(
                    "EXTERNAL_PROCESS_BLOCKED: " + event
                )

            if event.startswith("socket."):
                if event not in ("socket.__new__",):
                    raise AssertionError(
                        "SOCKET_OPERATION_BLOCKED: " + event
                    )

            paths = []

            if event == "open":
                path = args[0]
                mode = args[1] if len(args) > 1 else "r"
                flags = args[2] if len(args) > 2 else 0

                writing = (
                    isinstance(mode, str)
                    and any(c in mode for c in "wax+")
                )
                writing = writing or bool(
                    isinstance(flags, int)
                    and flags & (
                        os.O_WRONLY | os.O_RDWR |
                        os.O_CREAT | os.O_TRUNC | os.O_APPEND
                    )
                )
                if writing:
                    paths = [path]

            elif event in (
                "os.remove", "os.unlink",
                "os.mkdir", "os.rmdir",
                "os.chmod", "os.chown",
                "os.utime", "os.truncate",
            ):
                paths = [args[0]]

            elif event in ("os.rename", "os.replace"):
                paths = [args[0], args[1]]

            for value in paths:
                if isinstance(value, int):
                    fd_path = Path(f"/proc/self/fd/{value}")
                    try:
                        target = fd_path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        raise AssertionError(
                            "UNKNOWN_FILE_DESCRIPTOR"
                        )
                    if target != root and root not in target.parents:
                        raise AssertionError(
                            "FD_OUTSIDE_TEST_ROOT: " + str(target)
                        )
                    continue
                if not isinstance(value, (str, bytes, os.PathLike)):
                    raise AssertionError(
                        "UNKNOWN_MUTATION_PATH"
                    )
                target = Path(value).resolve()
                if target != root and root not in target.parents:
                    raise AssertionError(
                        "WRITE_OUTSIDE_TEST_ROOT: " + str(target)
                    )

        sys.addaudithook(audit_guard)

        now = int(time.time() * 1000)

        market = {
            "symbol": "TESTIRT",
            "status": "TRADING",
            "permissions": ["SPOT"],
        }

        score = {
            "symbol": "TESTIRT",
            "status": "WATCH",
            "price": 2000.0,
            "last_trade_time": now - 1000,
            "last_trade_price": 2000.0,
            "vr": 0.0,
            "va": 0.0,
            "bs": 0.0,
            "breakout": False,
            "pre_score": None,
        }

        calls = {"engine": 0, "alerts": 0, "saves": 0}
        original_engine = engine.run_comprehensive_v3_scan

        def monitored_engine(*args, **kwargs):
            calls["engine"] += 1
            assert kwargs["lab_state_dir"] == state
            assert kwargs["fetch_persistent_trades"] is False
            assert kwargs["verified_market_rows"] is None
            return original_engine(*args, **kwargs)

        def blocked_alert(*args, **kwargs):
            calls["alerts"] += 1
            raise AssertionError("ALERT_ATTEMPT")

        def fake_save(*args, **kwargs):
            calls["saves"] += 1

        with (
            patch.object(radar, "get_markets", return_value=[market]),
            patch.object(
                radar, "safe_score",
                return_value=(True, score, None),
            ),
            patch.object(radar, "load_persistence", return_value={}),
            patch.object(radar, "save_persistence", side_effect=fake_save),
            patch.object(radar, "save_dashboard_data", side_effect=fake_save),
            patch.object(radar, "save_ignition_history", side_effect=fake_save),
            patch.object(radar, "save_alert_history", side_effect=fake_save),
            patch.object(radar, "send_ntfy", side_effect=blocked_alert),
            patch.object(radar, "V3_LAB_HOOK_ENABLED", True),
            patch.object(radar, "V3_PRODUCTION_APPROVED", False),
            patch.object(
                radar, "get_v3_state_dir",
                return_value=state,
            ),
            patch.object(
                migration, "load_guarded_migration",
                return_value=({}, {"test": True}),
            ),
            patch.object(
                engine, "run_comprehensive_v3_scan",
                side_effect=monitored_engine,
            ),
        ):
            radar.run_once(
                max_markets=1,
                alerts_enabled=False,
                entry_v1_shadow=False,
                move_early_v2_shadow=False,
            )

        health_file = state / "health_latest.json"
        assert health_file.is_file(), "V3_HEALTH_MISSING"

        health = json.loads(health_file.read_text())

        assert health["status"] == "SUCCESS", health
        assert health["mode"] == "MONITOR_ONLY"
        assert health["real_alert_sent"] is False
        assert calls["engine"] == 1, calls
        assert calls["alerts"] == 0, calls

        print("V3_RUNONCE_INTEGRATION_PASS")
        print("HEALTH:", health["status"])
        print("ENGINE_CALLS:", calls["engine"])
        print("ALERT_ATTEMPTS:", calls["alerts"])
        print("NO_PRODUCTION_CHANGES")

if __name__ == "__main__":
    main()
