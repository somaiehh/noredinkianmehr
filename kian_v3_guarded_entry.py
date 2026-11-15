from pathlib import Path
from kian_v3_migration_guard import load_guarded_migration

BASE = Path.home() / "kian_v3_upgrade_lab/v3_main_integration"

def prepare_guarded_entry():
    waves, stats = load_guarded_migration()

    eligible = {}
    blocked = {}

    for wave_id, wave in waves.items():
        status = wave["migration_status"]

        if status == "HISTORICAL_MOVE_MONITOR_ONLY":
            eligible[wave_id] = wave
        else:
            blocked[wave_id] = status

    if len(eligible) + len(blocked) != len(waves):
        raise RuntimeError("MIGRATION_COVERAGE_MISMATCH")

    return eligible, blocked

if __name__ == "__main__":
    eligible, blocked = prepare_guarded_entry()

    print("=== V3 GUARDED ENTRY ===")
    print("TOTAL_WAVES:", len(eligible) + len(blocked))
    print("MONITOR_CANDIDATES:", len(eligible))
    print("BLOCKED_FROM_TRACKER_UPDATE:", len(blocked))
    print("REIGNITION_ALERTS_ENABLED:", False)
    print("TRACKER_WRITES:", 0)
    print("ENGINE_NOT_EXECUTED")
    print("NO_PRODUCTION_CHANGES")
