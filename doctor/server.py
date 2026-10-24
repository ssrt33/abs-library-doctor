"""Web page + JSON API."""
import logging
import os
import threading

from flask import Flask, jsonify, request, send_from_directory

from . import config, engine, fixes, store
from .checks import KINDS

app = Flask(__name__, static_folder=None)
STATIC = os.path.join(os.path.dirname(__file__), "static")
log = logging.getLogger("doctor")


def err(msg, code=400):
    return jsonify({"error": msg}), code


@app.errorhandler(LookupError)
def _missing(_):
    return err("Not found (it may have been fixed already – refresh)", 404)


@app.errorhandler(fixes.FixError)
def _fix_error(e):
    return err(str(e), 409)


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/api/state")
def state():
    s = store.snapshot()
    ok, why = fixes.can_move()
    if s.get("overlaps"):
        ok, why = False, "Library folders overlap."
    s["series"] = engine.dedupe_series(s.get("series") or [])  # tidy lists saved by older versions
    s["can_move"] = ok
    s["can_move_reason"] = why
    s["trash_mounted"] = os.path.ismount(config.TRASH_DIR)
    s["configured"] = bool(config.ABS_TOKEN)
    s["abs_url"] = config.ABS_URL
    s["kinds"] = {k: {"group": g, "label": l, "safe": safe, "about": about,
                      "moves_files": k in ("duplicate", "multi_disc")}
                  for k, (g, l, safe, about) in KINDS.items()}
    for h in s["history"]:
        h.pop("record", None)  # internal undo data
    return jsonify(s)


@app.post("/api/scan")
def scan_now():
    if store.get()["scan"]["running"]:
        return err("Already scanning", 409)
    threading.Thread(target=engine.scan, daemon=True).start()
    return jsonify({"started": True})


@app.post("/api/issues/<issue_id>/fix")
def fix(issue_id):
    body = request.get_json(silent=True) or {}
    return jsonify(engine.fix_issue(issue_id, keep=body.get("keep")) and {"ok": True})


@app.post("/api/issues/fix-kind/<kind>")
def fix_kind(kind):
    """Apply every open, non-ignored issue of one kind (only for fixes that don't move files)."""
    ids = [i["id"] for i in store.get()["issues"]
           if i["kind"] == kind and i["fix"] and not i.get("ignored") and i["fix"]["type"] not in engine.FILE_FIXES]
    done, failed = 0, []
    for i in ids:
        try:
            engine.fix_issue(i, rescan=False)
            done += 1
        except (fixes.FixError, LookupError) as e:
            failed.append(str(e))
    engine.wake()
    return jsonify({"fixed": done, "failed": failed})


@app.post("/api/issues/<issue_id>/ignore")
def ignore(issue_id):
    body = request.get_json(silent=True) or {}
    engine.ignore(issue_id, on=bool(body.get("on", True)))
    return jsonify({"ok": True})


@app.post("/api/history/<entry_id>/undo")
def undo(entry_id):
    force = bool((request.get_json(silent=True) or {}).get("force"))
    try:
        engine.undo(entry_id, force=force)
    except fixes.Stale as e:
        return jsonify({"error": str(e), "can_force": True}), 409
    return jsonify({"ok": True})


@app.post("/api/settings")
def settings():
    body = request.get_json(force=True) or {}
    with store.lock():
        s = store.get()["settings"]
        if "auto_fix" in body:
            s["auto_fix"] = bool(body["auto_fix"])
        if "scan_every_hours" in body:
            try:
                s["scan_every_hours"] = min(168.0, max(0.25, float(body["scan_every_hours"])))
            except (TypeError, ValueError):
                return err("Scan interval must be a number of hours")
        if "auto_kinds" in body:
            allowed = {k for k in KINDS if k not in ("duplicate", "multi_disc")}
            s["auto_kinds"] = [k for k in body["auto_kinds"] if k in allowed]
        store.save()
    if "scan_every_hours" in body:
        engine.wake()
    return jsonify(store.get()["settings"])


def main():
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("waitress").setLevel(logging.WARNING)
    store.load()
    if not config.ABS_TOKEN:
        log.warning("ABS_TOKEN isn't set. Create an API key in Audiobookshelf (Settings > API Keys) and set it.")
    ok, why = fixes.can_move()
    if not ok:
        log.warning("Fixes that move files are off: %s", why)
    elif not os.path.ismount(config.TRASH_DIR):
        log.warning("%s isn't a mounted folder; anything moved there is lost when the container is rebuilt.",
                    config.TRASH_DIR)
    threading.Thread(target=engine.scheduler, daemon=True).start()
    from waitress import serve
    log.info("Library Doctor on port %s, checking %s", config.PORT, config.ABS_URL)
    serve(app, host="0.0.0.0", port=config.PORT, threads=8)
