import json
from pathlib import Path
from collections import Counter

BASE = (
    Path.home()
    / "kian_v3_upgrade_lab"
    / "v3_main_integration"
)

SAFE_FILE = BASE / "v3_wave_migration_safe_v2.json"

def load_guarded_migration():
    data = json.loads(SAFE_FILE.read_text())

    if data.get("mode") != "V3_SAFE_MIGRATION_V2":
        raise ValueError("INVALID_MIGRATION_MODE")

    if data.get("production_enabled") is not False:
        raise ValueError("PRODUCTION_NOT_DISABLED")

    if data.get("real_alerts_enabled") is not False:
        raise ValueError("ALERTS_NOT_DISABLED")

    waves = data.get("waves")

    if not isinstance(waves, dict):
        raise ValueError("INVALID_WAVES")

    allowed_statuses = {
        "HISTORICAL_CORRECTION_UNVERIFIED",
        "HISTORICAL_MOVE_MONITOR_ONLY",
        "WAKE_ONLY_ARCHIVED",
    }

    stats = Counter()

    for wave_id, wave in waves.items():
        if not isinstance(wave, dict):
            raise ValueError("INVALID_WAVE")

        if wave_id != (
            f'{wave.get("symbol")}:'
            f'{wave.get("anchor_time")}'
        ):
            raise ValueError("WAVE_ID_MISMATCH")

        status = wave.get("migration_status")

        if status not in allowed_statuses:
            raise ValueError("UNSAFE_MIGRATION_STATUS")

        if wave.get("reignition_allowed") is not False:
            raise ValueError("REIGNITION_NOT_BLOCKED")

        stats[status] += 1

    return waves, stats

if __name__ == "__main__":
    waves, stats = load_guarded_migration()

    print("=== V3 MIGRATION GUARD ===")
    print("TOTAL_WAVES:", len(waves))

    for status, count in sorted(stats.items()):
        print(status + ":", count)

    print("REIGNITION_ALLOWED:", 0)
    print("TRACKER_UPDATES:", 0)
    print("MODE: MONITOR_ONLY")
    print("ENGINE_NOT_EXECUTED")
    print("NO_PRODUCTION_CHANGES")
