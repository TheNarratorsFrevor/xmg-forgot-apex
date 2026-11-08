"""Persistent daemon settings (what the user chose), not live readings.

Layout (v2):
    sync_power_profiles   follow/steer power-profiles-daemon (GNOME Power Mode)
    active_profile        power-saver | balanced | performance
    profiles.<name>.fan   mode auto|curve|manual, source, curve [[t,p]..], manual [p0,p1]
    keyboard.color        [r,g,b] or null (brightness is GNOME/UPower's job)
"""
from __future__ import annotations

import json
import os
import tempfile

from .presets import DEFAULT_PRESET_FOR_PROFILE, preset_curve

STATE_DIR = "/var/lib/xfa"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PROFILE_NAMES = ("power-saver", "balanced", "performance")


def default_fan(profile: str) -> dict:
    return {
        "mode": "auto",
        "source": "max",
        "curve": [list(p) for p in preset_curve(DEFAULT_PRESET_FOR_PROFILE[profile])],
        "manual": [40, 40],
    }


def defaults() -> dict:
    return {
        "version": 2,
        "sync_power_profiles": True,
        "active_profile": "balanced",
        "profiles": {name: {"fan": default_fan(name)} for name in PROFILE_NAMES},
        "keyboard": {"color": None},
    }


def load(path: str = STATE_FILE) -> dict:
    state = defaults()
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return state
    if not isinstance(data, dict):
        return state
    # v1 (single "fan" block) -> apply it to every profile
    if "fan" in data and "profiles" not in data:
        for name in PROFILE_NAMES:
            state["profiles"][name]["fan"].update({k: v for k, v in data["fan"].items() if k in state["profiles"][name]["fan"]})
    for name in PROFILE_NAMES:
        prof = (data.get("profiles") or {}).get(name)
        if isinstance(prof, dict) and isinstance(prof.get("fan"), dict):
            state["profiles"][name]["fan"].update({k: v for k, v in prof["fan"].items() if k in state["profiles"][name]["fan"]})
    if data.get("active_profile") in PROFILE_NAMES:
        state["active_profile"] = data["active_profile"]
    if isinstance(data.get("sync_power_profiles"), bool):
        state["sync_power_profiles"] = data["sync_power_profiles"]
    if isinstance(data.get("keyboard"), dict) and "color" in data["keyboard"]:
        state["keyboard"]["color"] = data["keyboard"]["color"]
    return state


def save(state: dict, path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".state-")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
