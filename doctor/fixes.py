"""Applying fixes, and undoing them.

Every apply returns a record with what it changed and the values from before, so it can be
undone. Fixes that touch files only ever move them (into the trash folder at worst); nothing is
deleted. Audiobookshelf items whose folders were moved away are removed from its database without
its "hard delete" option, so files are never touched by that either.
"""
import logging
import os
import shutil
import time

from . import config
from .checks import snapshot, view
from .textutil import safe_name

log = logging.getLogger("doctor")
FIELDS = ("title", "subtitle", "authors", "narrators", "series")


class FixError(Exception):
    pass


class Stale(FixError):
    """The book changed since the scan; re-scan before fixing."""


# ---------------------------------------------------------------- paths

def _maps():
    out = []
    for pair in config.PATH_MAP.split(","):
        if "=" in pair:
            a, b = pair.split("=", 1)
            out.append((a.strip().rstrip("/"), b.strip().rstrip("/")))
    return out


def to_local(abs_path):
    for a, b in _maps():
        if abs_path == a or abs_path.startswith(a + "/"):
            return b + abs_path[len(a):]
    return abs_path


def to_abs(local_path):
    for a, b in _maps():
        if local_path == b or local_path.startswith(b + "/"):
            return a + local_path[len(b):]
    return local_path


def can_move():
    """(ok, reason) for fixes that move files."""
    roots = [b for _a, b in _maps()]
    if not roots or not any(os.path.isdir(r) for r in roots):
        return False, "The library isn't mounted in this container (check PATH_MAP and volumes)."
    if not os.path.isdir(config.TRASH_DIR) or not os.access(config.TRASH_DIR, os.W_OK):
        return False, f"The trash folder {config.TRASH_DIR} is missing or not writable."
    return True, ""


def _chown(path):
    try:
        os.chown(path, config.PUID, config.PGID)
    except OSError:
        pass


def _makedirs(path):
    missing = []
    p = os.path.abspath(path)
    while not os.path.exists(p):
        missing.append(p)
        p = os.path.dirname(p)
    for p in reversed(missing):
        os.mkdir(p)
        _chown(p)


def _move(src, dst, moves):
    if os.path.exists(dst):
        raise FixError(f"Won't overwrite {dst}")
    _makedirs(os.path.dirname(dst))
    shutil.move(src, dst)
    moves.append([src, dst])


def _free_name(path):
    if not os.path.exists(path):
        return path
    for i in range(2, 100):
        p = f"{path} ({i})"
        if not os.path.exists(p):
            return p
    raise FixError(f"No free name near {path}")


def _trash_root():
    return os.path.join(config.TRASH_DIR, time.strftime("%Y-%m-%d %H%M%S"))


# ---------------------------------------------------------------- metadata

def _abs_payload(fields):
    md = {}
    for k, v in fields.items():
        if k in ("title", "subtitle"):
            md[k] = v
        elif k == "authors":
            md["authors"] = [{"name": n} for n in v]
        elif k == "narrators":
            md["narrators"] = list(v)
        elif k == "series":
            md["series"] = [{"name": n, "sequence": s} for n, s in v]
    return md


def _current(abs_api, item_id):
    it = abs_api.item(item_id)
    if not it or not isinstance(it, dict):
        raise Stale("That book is no longer in Audiobookshelf.")
    return view(it)


def apply_metadata(abs_api, fix):
    b = _current(abs_api, fix["item"])
    now = snapshot(b, fix["expect"].keys())
    if now != fix["expect"]:
        raise Stale(f"“{b['title']}” was changed since the last scan.")
    abs_api.update_metadata(b["id"], _abs_payload(fix["set"]))
    return {"type": "metadata", "item": b["id"], "title": b["title"], "before": now, "after": fix["set"]}


def undo_metadata(abs_api, rec, force=False):
    b = _current(abs_api, rec["item"])
    if not force and snapshot(b, rec["after"].keys()) != {k: _norm(v) for k, v in rec["after"].items()}:
        raise Stale(f"“{b['title']}” was edited after this fix, so undoing it could overwrite your changes.")
    abs_api.update_metadata(b["id"], _abs_payload(rec["before"]))


def _norm(v):
    return [list(x) for x in v] if v and isinstance(v, list) and isinstance(v[0], (list, tuple)) else v


# ---------------------------------------------------------------- covers

def apply_cover(abs_api, fix):
    b = _current(abs_api, fix["item"])
    if b["cover"]:
        raise Stale(f"“{b['title']}” has a cover now.")
    abs_api.set_cover_url(b["id"], fix["url"])
    return {"type": "cover", "item": b["id"], "title": b["title"], "url": fix["url"]}


def undo_cover(abs_api, rec, force=False):
    abs_api.remove_cover(rec["item"])


# ---------------------------------------------------------------- many at once

def apply_many(abs_api, fix):
    done = []
    try:
        for op in fix["ops"]:
            done.append(apply_metadata(abs_api, op))
    except FixError:
        for rec in reversed(done):
            try:
                undo_metadata(abs_api, rec, force=True)
            except Exception as e:  # best effort
                log.warning("Rollback failed: %s", e)
        raise
    return {"type": "many", "records": done}


def undo_many(abs_api, rec, force=False):
    for r in reversed(rec["records"]):
        undo_metadata(abs_api, r, force=force)


# ---------------------------------------------------------------- waiting on scans

def _items_by_path(abs_api, library_id):
    res = abs_api._req("GET", f"/api/libraries/{library_id}/items?limit=0&minified=1", timeout=120)
    return {it["path"]: it for it in res.get("results", [])}


def _scan_and_wait(abs_api, library_id, want):
    """Scan, then wait until every abs path in want {path: n_audio_files or None} shows up."""
    abs_api.scan_library(library_id)

    def ready():
        items = _items_by_path(abs_api, library_id)
        for p, n in want.items():
            it = items.get(p)
            if not it or it.get("isMissing") or (n is not None and it["media"].get("numAudioFiles") != n):
                return None
        return items
    items = abs_api.wait_for(ready, timeout=300)
    if not items:
        raise FixError("Moved the files, but Audiobookshelf didn't pick up the change in time. "
                       "Run a library scan in Audiobookshelf; nothing was lost.")
    return items


def _forget_missing(abs_api, ids, keep_id=None):
    for i in ids:
        if i == keep_id:
            continue
        try:
            it = abs_api.item(i)
        except Exception:
            continue  # already gone
        if isinstance(it, dict) and (it.get("isMissing") or not os.path.exists(to_local(it["path"]))):
            abs_api.forget_item(i)


def _restore_meta(abs_api, item_id, meta):
    abs_api.update_metadata(item_id, _abs_payload(meta))


# ---------------------------------------------------------------- merge discs

def apply_merge(abs_api, fix):
    ok, why = can_move()
    if not ok:
        raise FixError(why)
    books = [_current(abs_api, i) for i in fix["order"]]
    for b in books:
        if b["missing"] or b["is_file"] or not b["files"]:
            raise Stale(f"“{b['rel']}” can't be merged right now.")
        if not os.path.isdir(to_local(b["path"])):
            raise FixError(f"Can't see {to_local(b['path'])} from this container (check PATH_MAP).")
    keep = books[0]
    keep_dir = to_local(keep["path"])
    # The merged book goes in Author/Title at the top of the library folder.
    root = to_local(keep["path"][:len(keep["path"]) - len(keep["rel"])].rstrip("/")) if keep["rel"] else \
        os.path.dirname(keep_dir)
    parts = [safe_name(fix.get("author") or "")] if fix.get("author") else []
    target = os.path.join(root, *parts, safe_name(fix["title"], "Merged book"))
    if target != keep_dir:
        target = _free_name(target)
    old_parents = {os.path.dirname(to_local(b["path"])) for b in books} - {root}
    trash = os.path.join(_trash_root(), "leftovers")
    moves, removed_dirs, plan_names = [], [], []
    meta = {b["id"]: {**snapshot(b, FIELDS), "path": b["path"]} for b in books}
    try:
        for d, b in enumerate(books, 1):
            src_dir = to_local(b["path"])
            for f in b["files"]:
                name = f["name"] if f["name"].lower().startswith(f"disc {d:02d} - ") else f"Disc {d:02d} - {f['name']}"
                dst = os.path.join(keep_dir, name)
                src = to_local(f["path"])
                if src != dst:
                    _move(src, dst, moves)
                plan_names.append(name)
            if b is not keep:
                for left in sorted(os.listdir(src_dir)):  # covers, metadata files… set aside, not deleted
                    _move(os.path.join(src_dir, left), os.path.join(trash, b["rel"], left), moves)
                os.rmdir(src_dir)
                removed_dirs.append(src_dir)
        if target != keep_dir:
            _move(keep_dir, target, moves)
        for d in sorted(old_parents, key=len, reverse=True):  # e.g. an emptied "Author read by X" folder
            if os.path.isdir(d) and not os.listdir(d) and d != os.path.dirname(target):
                os.rmdir(d)
                removed_dirs.append(d)
    except Exception as e:
        _rollback(moves, removed_dirs)
        raise FixError(f"Merge stopped and was rolled back: {e}") from e

    target_abs = to_abs(target)
    items = _scan_and_wait(abs_api, keep["library"], {target_abs: len(plan_names)})
    merged = items[target_abs]
    full = abs_api.item(merged["id"])
    inos = {a["metadata"]["filename"]: a["ino"] for a in full["media"]["audioFiles"]}
    abs_api.set_track_order(merged["id"], [inos[n] for n in plan_names if n in inos])
    if merged["id"] != keep["id"]:  # Audiobookshelf made a new item; give it the old details
        _restore_meta(abs_api, merged["id"], meta[keep["id"]])
    _forget_missing(abs_api, [b["id"] for b in books], keep_id=merged["id"])
    return {"type": "merge", "library": keep["library"], "item": merged["id"], "title": fix["title"],
            "moves": moves, "removed_dirs": removed_dirs, "meta": meta, "files": plan_names}


def _rollback(moves, removed_dirs):
    for d in removed_dirs:
        _makedirs(d)
    for src, dst in reversed(moves):
        if os.path.exists(dst) and not os.path.exists(src):
            _makedirs(os.path.dirname(src))
            shutil.move(dst, src)


def undo_merge(abs_api, rec, force=False):
    _rollback(rec["moves"], rec["removed_dirs"])
    want = {m["path"]: None for m in rec["meta"].values()}
    items = _scan_and_wait(abs_api, rec["library"], want)
    for m in rec["meta"].values():
        it = items[m["path"]]
        _restore_meta(abs_api, it["id"], {k: m[k] for k in FIELDS})
    if rec["item"] not in {items[m["path"]]["id"] for m in rec["meta"].values()}:
        _forget_missing(abs_api, [rec["item"]])


# ---------------------------------------------------------------- duplicates

def apply_dedupe(abs_api, fix):
    ok, why = can_move()
    if not ok:
        raise FixError(why)
    keep = _current(abs_api, fix["keep"])
    others = [_current(abs_api, i) for i in fix["remove"]]
    root = os.path.join(_trash_root(), "duplicates")
    moves, meta = [], {}
    try:
        for b in others:
            src = to_local(b["path"])
            if not os.path.exists(src):
                raise FixError(f"Can't see {src} from this container (check PATH_MAP).")
            _move(src, os.path.join(root, b["rel"]), moves)
            meta[b["id"]] = {**snapshot(b, FIELDS), "path": b["path"]}
    except Exception as e:
        _rollback(moves, [])
        raise FixError(f"Stopped and put everything back: {e}") from e
    _scan_and_wait(abs_api, keep["library"], {keep["path"]: None})
    _forget_missing(abs_api, [b["id"] for b in others])
    return {"type": "dedupe", "library": keep["library"], "keep": keep["id"], "title": keep["title"],
            "moves": moves, "meta": meta, "trash": root}


def undo_dedupe(abs_api, rec, force=False):
    _rollback(rec["moves"], [])
    items = _scan_and_wait(abs_api, rec["library"], {m["path"]: None for m in rec["meta"].values()})
    for m in rec["meta"].values():
        _restore_meta(abs_api, items[m["path"]]["id"], {k: m[k] for k in FIELDS})


APPLY = {"metadata": apply_metadata, "cover": apply_cover, "metadata_many": apply_many,
         "merge": apply_merge, "dedupe": apply_dedupe}
UNDO = {"metadata": undo_metadata, "cover": undo_cover, "many": undo_many, "merge": undo_merge,
        "dedupe": undo_dedupe}


def apply(abs_api, fix):
    return APPLY[fix["type"]](abs_api, fix)


def undo(abs_api, rec, force=False):
    return UNDO[rec["type"]](abs_api, rec, force=force)
