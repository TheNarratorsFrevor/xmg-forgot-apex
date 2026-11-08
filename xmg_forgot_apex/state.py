"""Persistent daemon settings (what the user chose), not live readings."""
from __future__ import annotations

import json
import os
import tempfile

from .fan import DEFAULT_CURVE

STATE_DIR = "/var/lib/xfa"
STATE_FILE = os.path.join(STATE_DIR, "state.json")

DEFAULTS = {
    "fan": {
        "mode": "auto",
        "source": "max",
        "curve": [list(p) for p in DEFAULT_CURVE],
        "manual": [40, 40],
    },
    "keyboard": {
        "brightness": None,   # None = leave whatever the firmware/driver set
        "color": None,        # [r, g, b] 0..255 or None
    },
}


def load(path: str = STATE_FILE) -> dict:
    state = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return state
    for section in ("fan", "keyboard"):
        if isinstance(data.get(section), dict):
            state[section].update(data[section])
    return state


def save(state: dict, path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".state-")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
