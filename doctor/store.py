"""App state, kept in one JSON file under CONFIG_DIR."""
import copy
import json
import os
import threading
import time

from . import config
from .checks import KINDS

_lock = threading.RLock()
_state = None


def _path():
    return os.path.join(config.CONFIG_DIR, "state.json")


def _defaults():
    return {
        "settings": {
            "auto_fix": config.AUTO_FIX,
            "scan_every_hours": config.SCAN_EVERY_HOURS,
            "auto_kinds": [k for k, v in KINDS.items() if v[2]],
        },
        "issues": [],        # open issues from the last scan
        "ignored": {},       # issue id -> {summary, kind, at}
        "history": [],       # applied fixes, newest first
        "series": [],
        "errors": [],
        "last_scan": None,
        "scan": {"running": False, "step": "", "error": None},
        "books": 0,
        "server": None,
    }


def load():
    global _state
    with _lock:
        if _state is None:
            os.makedirs(config.CONFIG_DIR, exist_ok=True)
            st = _defaults()
            try:
                with open(_path()) as f:
                    saved = json.load(f)
                for k in st:
                    if k in saved:
                        st[k] = saved[k]
                st["settings"] = {**_defaults()["settings"], **saved.get("settings", {})}
            except (OSError, ValueError):
                pass
            st["scan"] = {"running": False, "step": "", "error": None}
            _state = st
        return _state


def save():
    with _lock:
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f, indent=1)
        os.replace(tmp, _path())


def lock():
    return _lock


def get():
    return load()


def snapshot():
    with _lock:
        return copy.deepcopy(load())


def add_history(entry):
    with _lock:
        st = load()
        entry.setdefault("at", time.time())
        st["history"].insert(0, entry)
        del st["history"][500:]
        save()
