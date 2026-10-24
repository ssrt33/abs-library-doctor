"""Scanning, auto-fixing, applying and undoing, and the background schedule."""
import logging
import threading
import time
import uuid

from . import checks, config, fixes, store
from .abs_client import ABS, ABSError

log = logging.getLogger("doctor")
_run_lock = threading.Lock()   # one scan or fix at a time
_wake = threading.Event()
FILE_FIXES = ("merge", "dedupe")  # these move files, so they always wait for a click


def _libraries(abs_api):
    libs = [l for l in abs_api.libraries() if l.get("mediaType") == "book"]
    if config.LIBRARIES:
        want = {x.lower() for x in config.LIBRARIES}
        libs = [l for l in libs if l["id"].lower() in want or l["name"].lower() in want]
    return libs


def _in_progress(abs_api):
    try:
        return {p["libraryItemId"] for p in abs_api.me().get("mediaProgress", [])
                if not p.get("isFinished") and (p.get("currentTime") or 0) > 0}
    except ABSError:
        return set()


def _step(text):
    with store.lock():
        store.get()["scan"]["step"] = text


def _check_all(abs_api, lookups=True):
    items, libs = [], _libraries(abs_api)
    for lib in libs:
        _step(f"Reading {lib['name']}")
        items += abs_api.items_full(abs_api.item_ids(lib["id"]))
    _step(f"Checking {len(items)} books")
    ok, _why = fixes.can_move()
    overlaps = checks.folder_overlaps(libs)
    if overlaps:
        ok = False  # moving files while folders overlap could take the only real copy
    res = checks.run(items, abs_api, in_progress=_in_progress(abs_api), can_move=ok, lookups=lookups)
    res["libraries"] = [{"id": l["id"], "name": l["name"], "folders": [f["fullPath"] for f in l.get("folders") or []]}
                        for l in libs]
    res["overlaps"] = overlaps
    return res


def series_row_key(r):
    """One key per series however it was spelled ("The Enemy", "the enemy", "Enemy Series")."""
    return checks.series_key(r.get("key") or r["name"])


def dedupe_series(rows):
    """Collapse rows for the same series, preferring a fresh one over one kept from before."""
    best = {}
    for r in rows:
        k = series_row_key(r)
        if k not in best or (best[k].get("stale") and not r.get("stale")):
            best[k] = r
    return sorted(best.values(), key=lambda r: r["name"].lower())


def merge_series(old, new):
    """Keep the series list steady between checks.

    A check can catch Audiobookshelf mid-rescan and see only some books, or Audible can fail to
    answer. Neither should wipe what you were looking at, so:
      - a series that vanishes stays listed until it has been missing from two checks in a row;
      - a series whose Audible lookup failed keeps its last good list of missing books.
    """
    prev = {}
    for r in dedupe_series(old):
        prev[series_row_key(r)] = r
    out, seen = [], set()
    for r in new:
        k = series_row_key(r)
        seen.add(k)
        p = prev.get(k)
        if r.get("lookup_failed") and p and p.get("found"):
            r = {**p, "owned": r["owned"], "name": r["name"], "key": k, "stale": True}
        else:
            r = {**r, "key": k}
        r.pop("absent", None)
        out.append(r)
    for k, p in prev.items():
        if k in seen:
            continue
        absent = p.get("absent", 0) + 1
        if absent < 2:
            out.append({**p, "key": k, "absent": absent, "stale": True})
    return dedupe_series(out)


def _publish(res):
    with store.lock():
        st = store.get()
        ignored = st["ignored"]
        for i in res["issues"]:
            i["ignored"] = i["id"] in ignored
        st["issues"] = res["issues"]
        st["series"] = merge_series(st.get("series") or [], res["series"])
        st["errors"] = res["errors"]
        st["books"] = res["books"]
        st["libraries"] = res.get("libraries", [])
        st["overlaps"] = res.get("overlaps", [])
        st["last_scan"] = time.time()
        store.save()


def _record(issue, rec, auto):
    entry = {"id": uuid.uuid4().hex[:10], "issue": issue["id"], "kind": issue["kind"], "summary": issue["summary"],
             "changes": issue["changes"], "plan": issue["plan"], "auto": auto, "record": rec, "undone": False}
    store.add_history(entry)
    return entry


def scan(auto_fix=None):
    """Read the library, run the checks, apply safe fixes if enabled. Returns a summary."""
    if not _run_lock.acquire(blocking=False):
        return {"busy": True}
    abs_api = ABS()
    fixed, failed = [], []
    try:
        with store.lock():
            st = store.get()
            st["scan"] = {"running": True, "step": "Connecting", "error": None}
            settings = dict(st["settings"])
        st_status = abs_api.status()
        with store.lock():
            store.get()["server"] = {"version": st_status.get("serverVersion"), "url": abs_api.url}
        res = _check_all(abs_api)
        auto = settings["auto_fix"] if auto_fix is None else auto_fix
        if auto:
            ignored = store.get()["ignored"]
            for _pass in range(8):
                todo = [i for i in res["issues"] if i["fix"] and i["safe"] and i["kind"] in settings["auto_kinds"]
                        and i["id"] not in ignored and i["fix"]["type"] not in FILE_FIXES]
                if not todo:
                    break
                touched, applied_any = set(), False
                for issue in todo:
                    ids = {b["id"] for b in issue["books"]}
                    if ids & touched:
                        continue  # one change per book per pass; the rest gets re-checked
                    touched |= ids
                    _step(f"Fixing: {issue['summary']}")
                    try:
                        rec = fixes.apply(abs_api, issue["fix"])
                        _record(issue, rec, auto=True)
                        fixed.append(issue["summary"])
                        applied_any = True
                    except fixes.Stale:
                        continue
                    except Exception as e:
                        log.warning("Auto-fix failed for %s: %s", issue["summary"], e)
                        failed.append(f"{issue['summary']}: {e}")
                        ignored_now = store.get()["ignored"]
                        ignored_now[issue["id"]] = {"summary": issue["summary"], "kind": issue["kind"],
                                                    "at": time.time(), "reason": f"Auto-fix failed: {e}"}
                if not applied_any:
                    break
                res = _check_all(abs_api)
        _publish(res)
        if fixed:
            log.info("Auto-fixed %d issue(s)", len(fixed))
            _notify(fixed, res)
        return {"fixed": fixed, "failed": failed, "issues": len(res["issues"])}
    except Exception as e:
        log.exception("Scan failed")
        with store.lock():
            store.get()["scan"]["error"] = str(e)
        return {"error": str(e)}
    finally:
        with store.lock():
            st = store.get()
            st["scan"]["running"] = False
            st["scan"]["step"] = ""
            store.save()
        _run_lock.release()


def _notify(fixed, res):
    if not config.NOTIFY_URL:
        return
    open_n = sum(1 for i in res["issues"] if i["fix"] and not i.get("ignored"))
    from .util import notify
    notify("Library Doctor", f"Fixed {len(fixed)} thing(s) on its own. {open_n} waiting for you.")


def fix_issue(issue_id, rescan=True, keep=None):
    """Apply one open issue's fix (from the page)."""
    with store.lock():
        issue = next((i for i in store.get()["issues"] if i["id"] == issue_id), None)
    if not issue or not issue["fix"]:
        raise LookupError
    if not _run_lock.acquire(timeout=600):
        raise fixes.FixError("Another scan or fix is still running.")
    abs_api = ABS()
    try:
        with store.lock():
            store.get()["scan"] = {"running": True, "step": f"Fixing: {issue['summary']}", "error": None}
        fix = dict(issue["fix"])
        if keep and fix["type"] == "dedupe":
            ids = [fix["keep"]] + fix["remove"]
            if keep not in ids:
                raise fixes.FixError("That copy isn't part of this issue.")
            fix["keep"], fix["remove"] = keep, [i for i in ids if i != keep]
            issue = dict(issue, plan=[p for p in issue["plan"]])  # history shows what was really done
            rels = {c["id"]: c["rel"] for c in issue.get("copies", [])}
            issue["plan"] = [f"Keep: {rels.get(keep, keep)}"] + [f"Move to trash: {rels.get(i, i)}" for i in fix["remove"]]
        rec = fixes.apply(abs_api, fix)
        entry = _record(issue, rec, auto=False)
        with store.lock():
            st = store.get()
            st["issues"] = [i for i in st["issues"] if i["id"] != issue_id]
            store.save()
        return entry
    finally:
        with store.lock():
            store.get()["scan"]["running"] = False
        _run_lock.release()
        if rescan:
            wake()  # re-check; one fix can clear (or reveal) others


def undo(entry_id, force=False):
    with store.lock():
        entry = next((h for h in store.get()["history"] if h["id"] == entry_id), None)
    if not entry or entry["undone"]:
        raise LookupError
    if not _run_lock.acquire(timeout=600):
        raise fixes.FixError("Another scan or fix is still running.")
    abs_api = ABS()
    try:
        with store.lock():
            store.get()["scan"] = {"running": True, "step": f"Undoing: {entry['summary']}", "error": None}
        fixes.undo(abs_api, entry["record"], force=force)
        with store.lock():
            entry["undone"] = True
            entry["undone_at"] = time.time()
            # don't let auto-fix redo it straight away
            store.get()["ignored"][entry["issue"]] = {"summary": entry["summary"], "kind": entry["kind"],
                                                      "at": time.time(), "reason": "You undid this fix."}
            for h in store.get()["history"]:
                if h["id"] == entry_id:
                    h.update(entry)
            store.save()
    finally:
        with store.lock():
            store.get()["scan"]["running"] = False
        _run_lock.release()
        wake()


def ignore(issue_id, on=True):
    with store.lock():
        st = store.get()
        if on:
            issue = next((i for i in st["issues"] if i["id"] == issue_id), None)
            if not issue:
                raise LookupError
            st["ignored"][issue_id] = {"summary": issue["summary"], "kind": issue["kind"], "at": time.time()}
            issue["ignored"] = True
        else:
            st["ignored"].pop(issue_id, None)
            for i in st["issues"]:
                if i["id"] == issue_id:
                    i["ignored"] = False
        store.save()


def wake():
    _wake.set()


def scheduler():
    """Scan at start-up and then every N hours (from settings)."""
    time.sleep(3)
    while True:
        if config.ABS_TOKEN:
            scan()
        hours = max(0.25, float(store.get()["settings"].get("scan_every_hours") or 6))
        _wake.wait(timeout=hours * 3600)
        _wake.clear()
