from pathlib import Path

V3_MODE = "MONITOR_ONLY"
V3_PRODUCTION_APPROVED = False

def get_v3_state_dir(mode="lab"):
    if mode == "lab":
        return (
            Path.home()
            / "kian_v3_upgrade_lab"
            / "v3_main_integration"
            / "v3_persistent_state"
        )

    if mode == "staging":
        # V3_PORTABLE_STAGING_STATE_V1
        # Monitor-only state; production approval remains disabled.
        return Path(__file__).resolve().parent / "v3_state"

    if mode == "production":
        if not V3_PRODUCTION_APPROVED:
            raise RuntimeError("PRODUCTION_STATE_NOT_APPROVED")
        state = (Path(__file__).resolve().parent / "v3_state").resolve()
        if state == Path.cwd().resolve():
            raise RuntimeError("INVALID_V3_STATE_ROOT")
        return state

    raise ValueError("INVALID_V3_MODE")
