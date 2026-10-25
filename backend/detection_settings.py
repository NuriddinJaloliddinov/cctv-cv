"""Persisted global and per-camera CV detection switches.

Global Off is a safety gate: camera-specific overrides cannot turn a globally
disabled feature back on. Turning off people also pauses every dependent
feature (identity, phone-use, and entrance/exit crossing).
"""
import json
import threading

from . import db


KEY = "detection_policy"
_lock = threading.Lock()


def policy():
    raw = db.get_setting(KEY)
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                global_value = stored.get("global", {})
                overrides = stored.get("overrides", {})
                if (isinstance(global_value, dict) and isinstance(overrides, dict)
                        and all(isinstance(global_value.get(k), bool)
                                for k in ("person", "phone"))):
                    return {"global": global_value, "overrides": overrides}
        except (ValueError, TypeError):
            pass
    return {"global": {"person": True, "phone": True}, "overrides": {}}


def effective(camera_id, settings=None):
    settings = settings if settings is not None else policy()
    master = settings["global"]
    local = settings["overrides"].get(camera_id, {})
    person = bool(master["person"] and local.get("person", True))
    phone = bool(person and master["phone"] and local.get("phone", True))
    return {"person": person, "phone": phone}


def save(scope, person, phone, camera_ids=()):
    if (scope not in ("global", "exclusive") or
            not isinstance(person, bool) or not isinstance(phone, bool)):
        raise ValueError("Invalid detection settings")
    with _lock:
        settings = policy()
        if scope == "global":
            settings = {"global": {"person": person, "phone": phone},
                        "overrides": {}}
        else:
            if not camera_ids:
                raise ValueError("Choose at least one camera")
            for camera_id in camera_ids:
                settings["overrides"][camera_id] = {
                    "person": person, "phone": phone}
        db.set_settings({KEY: json.dumps(settings)})
    return settings


def remove_camera(camera_id):
    with _lock:
        settings = policy()
        if camera_id in settings["overrides"]:
            settings["overrides"].pop(camera_id)
            db.set_settings({KEY: json.dumps(settings)})
