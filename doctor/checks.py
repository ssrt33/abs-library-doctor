"""The checks. Each one looks at the library and returns issues, each with a ready-made fix.

An issue is a plain dict:
  id       stable across scans, so "ignore" sticks
  kind     which check found it
  group    "Metadata", "Duplicates & discs" or "Series"
  summary  one line for the list
  books    [{id, title, author}] the books involved
  changes  [{field, before, after}] what the fix will change, for the preview
  plan     extra lines describing file moves, for fixes that touch files
  safe     True if the fix is low-risk enough to apply on its own
  fix      what to do (see fixes.py), or None if it's information only
"""
import hashlib
import logging
import re
from collections import defaultdict

from . import audible
from .textutil import (RANGE, clean_seq, credential_text, disc_number, fold, has_unabridged, is_credential,
                       natural_key, safe_name, series_key, seq_sort, split_read_by, strip_unabridged, title_key)

log = logging.getLogger("doctor")
COPY_NAME = re.compile(r"\bcopy\b|\(\d+\)\s*$|\bduplicate\b", re.I)

# kind -> (group, label, safe by default, description)
KINDS = {
    "unabridged": ("Metadata", "“(Unabridged)” in titles", True,
                   "Removes “(Unabridged)” and similar tags from the title and subtitle."),
    "title_subtitle": ("Metadata", "Subtitle repeated in the title", True,
                       "“Title: Subtitle” with the same subtitle in its own field becomes just “Title”."),
    "narrator_in_author": ("Metadata", "Narrator stuck in the author field", True,
                           "“John Grisham read by …” becomes author John Grisham, narrator …"),
    "author_credential": ("Metadata", "Author split at a credential", True,
                          "“Judson Somerville” + “Md” become one author, “Judson Somerville MD”."),
    "series_suffix": ("Series", "Same series under two names", True,
                      "“Mitch Rapp Series” is renamed to “Mitch Rapp” when that series already exists."),
    "series_number_missing": ("Series", "Missing series number", True,
                              "Fills in the book's number from the series list on Audible, matched by title."),
    "missing_cover": ("Metadata", "Missing cover", True,
                      "Sets a cover from Audiobookshelf's lookup when the title and author match exactly."),
    "series_is_subtitle": ("Series", "Subtitle used as the series", False,
                           "The series field holds the subtitle. Uses the real series from Audible, or clears it."),
    "series_number_mismatch": ("Series", "Series number differs from Audible", False,
                               "Your number differs from Audible's. Left for you, since reading order can differ."),
    "volume_set": ("Series", "Volumes listed as separate books", False,
                   "Groups numbered volumes (e.g. Pimsleur lessons 1-5, 6-10…) into one series, in order."),
    "duplicate": ("Duplicates & discs", "Duplicate copies", False,
                  "Keeps the best copy and moves the others to the trash folder (not deleted)."),
    "multi_disc": ("Duplicates & discs", "One book split into discs", False,
                   "Moves the discs' audio into one folder, in disc order, so it's a single book."),
}


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def view(it):
    """Flatten an Audiobookshelf item into what the checks need."""
    md = it["media"]["metadata"]
    files = sorted(it["media"].get("audioFiles") or [], key=lambda a: a.get("index", 0))
    return {
        "id": it["id"],
        "library": it["libraryId"],
        "path": it["path"],
        "rel": it.get("relPath") or "",
        "is_file": it.get("isFile", False),
        "missing": it.get("isMissing", False) or it.get("isInvalid", False),
        "title": md.get("title") or "",
        "subtitle": md.get("subtitle") or "",
        "authors": [a["name"] for a in md.get("authors") or []],
        "narrators": list(md.get("narrators") or []),
        "series": [[s["name"], s.get("sequence") or ""] for s in md.get("series") or []],
        "asin": md.get("asin") or "",
        "cover": bool(it["media"].get("coverPath")),
        "duration": it["media"].get("duration") or 0,
        "size": it["media"].get("size") or it.get("size") or 0,
        "files": [{"ino": a["ino"], "name": a["metadata"]["filename"], "path": a["metadata"]["path"]} for a in files],
        "durs": [round(a.get("duration") or 0, 1) for a in files],
        "added": it.get("addedAt") or 0,
        "desc": bool(md.get("description")),
    }


def snapshot(b, fields):
    return {f: (list(map(list, b[f])) if f == "series" else list(b[f]) if isinstance(b[f], list) else b[f])
            for f in fields}


def book_ref(b):
    return {"id": b["id"], "title": b["title"], "author": ", ".join(b["authors"])}


def _issue(kind, books, summary, changes=None, fix=None, safe=None, plan=None, note=None, key=""):
    group, _label, default_safe, _ = KINDS[kind]
    return {
        "id": _id(kind, key or ",".join(sorted(b["id"] for b in books))),
        "kind": kind,
        "group": group,
        "summary": summary,
        "books": [book_ref(b) for b in books],
        "changes": changes or [],
        "plan": plan or [],
        "safe": default_safe if safe is None else safe,
        "fix": fix,
        "note": note,
    }


def _meta_fix(b, new):
    """A metadata fix that only goes ahead if the fields still hold what we saw."""
    return {"type": "metadata", "item": b["id"], "set": new, "expect": snapshot(b, new.keys())}


def _series_txt(series):
    return "; ".join(f"{n} #{s}" if s else n for n, s in series) or "(none)"


# ---------------------------------------------------------------- metadata checks

def check_unabridged(b):
    new = {}
    if has_unabridged(b["title"]):
        new["title"] = strip_unabridged(b["title"])
    if has_unabridged(b["subtitle"]):
        new["subtitle"] = strip_unabridged(b["subtitle"])
    if not new:
        return None
    ch = [{"field": k.title(), "before": b[k], "after": v} for k, v in new.items()]
    return _issue("unabridged", [b], f"{b['title']} → {new.get('title', b['title'])}", ch, _meta_fix(b, new))


def check_title_subtitle(b):
    sub = strip_unabridged(b["subtitle"]).strip()
    title = strip_unabridged(b["title"])
    if not sub or len(sub) < 3:
        return None
    m = re.match(rf"^(.+?)\s*(?::|\s-|–|—)\s*{re.escape(sub)}\s*$", title, re.I)
    if not m or not m.group(1).strip():
        return None
    new_title = m.group(1).strip()
    if has_unabridged(b["title"]):  # leave the unabridged fix to its own check, one change at a time
        return None
    return _issue("title_subtitle", [b], f"{b['title']} → {new_title}",
                  [{"field": "Title", "before": b["title"], "after": new_title}], _meta_fix(b, {"title": new_title}))


def check_narrator_in_author(b):
    out_authors, found = [], []
    for a in b["authors"]:
        sp = split_read_by(a)
        if sp:
            out_authors.append(sp[0])
            found.append(sp[1])
        else:
            out_authors.append(a)
    if not found:
        return None
    narrators = list(b["narrators"])
    for n in found:
        for part in re.split(r"\s*(?:,|&|\band\b)\s*", n):
            if part and fold(part) not in {fold(x) for x in narrators}:
                narrators.append(part)
    out_authors = list(dict.fromkeys(out_authors))
    new = {"authors": out_authors, "narrators": narrators}
    ch = [{"field": "Author", "before": ", ".join(b["authors"]), "after": ", ".join(out_authors)},
          {"field": "Narrator", "before": ", ".join(b["narrators"]) or "(none)", "after": ", ".join(narrators)}]
    return _issue("narrator_in_author", [b], f"{b['title']}: author “{', '.join(b['authors'])}”", ch, _meta_fix(b, new))


def check_author_credential(b):
    if len(b["authors"]) < 2 or not any(is_credential(a) for a in b["authors"][1:]):
        return None
    out = []
    for a in b["authors"]:
        if is_credential(a) and out:
            out[-1] = f"{out[-1]} {credential_text(a)}"
        else:
            out.append(a)
    return _issue("author_credential", [b], f"{b['title']}: “{', '.join(b['authors'])}”",
                  [{"field": "Author", "before": ", ".join(b["authors"]), "after": ", ".join(out)}],
                  _meta_fix(b, {"authors": out}))


def check_series_suffix(b, names_by_key):
    new, changed = [], False
    for name, seq in b["series"]:
        k = series_key(name)
        others = [n for n in names_by_key.get(k, ()) if n != name]
        # rename only toward a name that's the plain form (no "Series"/"Saga" suffix)
        plain = [n for n in others if fold(n) == k or fold(n) == "the " + k]
        if plain and fold(name) != fold(plain[0]):
            new.append([plain[0], seq])
            changed = True
        else:
            new.append([name, seq])
    if not changed:
        return None
    return _issue("series_suffix", [b], f"{b['title']}: “{_series_txt(b['series'])}”",
                  [{"field": "Series", "before": _series_txt(b["series"]), "after": _series_txt(new)}],
                  _meta_fix(b, {"series": new}))


def bogus_series(b):
    """Series entries that are really the subtitle (or the title)."""
    # Only the subtitle counts: plenty of real series share the first book's title (Legion #1).
    sub = fold(strip_unabridged(b["subtitle"]))
    return [name for name, _seq in b["series"] if sub and fold(name) == sub]


def check_series_is_subtitle(b, lookups):
    bad = bogus_series(b)
    if not bad:
        return None
    keep = [s for s in b["series"] if s[0] not in bad]
    found = None
    if lookups:
        try:
            p = audible.find_book(b["title"], b["authors"][0] if b["authors"] else "")
            if p and p["series"]:
                s = p["series"][0]
                found = [s["title"], s["sequence"]]
        except Exception as e:
            log.info("Audible lookup failed for %s: %s", b["title"], e)
    new = keep + ([found] if found and series_key(found[0]) not in {series_key(n) for n, _ in keep} else [])
    note = None if found else "Audible didn't list this book in a series, so the fix just clears it."
    return _issue("series_is_subtitle", [b], f"{b['title']}: series “{bad[0]}”",
                  [{"field": "Series", "before": _series_txt(b["series"]), "after": _series_txt(new)}],
                  _meta_fix(b, {"series": new}), note=note)


def check_missing_cover(b, abs_api, lookups):
    if b["cover"] or b["missing"] or not lookups:
        return None
    author = b["authors"][0] if b["authors"] else ""
    if split_read_by(author):  # wait until the author is fixed, or the lookup will miss
        return None
    try:
        results = audible._cached(f"abscover:{fold(title_key(b['title']))}|{fold(author)}",
                                  lambda: [{"title": r.get("title"), "author": r.get("author"), "cover": r.get("cover")}
                                           for r in abs_api.search_books(title_key(b["title"]) or b["title"], author)
                                           if r.get("cover")], ttl=24 * 3600)
    except Exception as e:
        log.info("Cover lookup failed for %s: %s", b["title"], e)
        return None
    if not results:
        return _issue("missing_cover", [b], f"{b['title']}: no cover", safe=False,
                      note="No cover found by the lookup. Add one in Audiobookshelf (Edit > Cover).")
    exact = [r for r in results if title_key(r["title"]) == title_key(b["title"])
             and (not author or fold(author) in fold(r.get("author") or ""))]
    pick = (exact or results)[0]
    return _issue("missing_cover", [b], f"{b['title']}: no cover",
                  [{"field": "Cover", "before": "(none)", "after": pick["cover"], "image": True}],
                  {"type": "cover", "item": b["id"], "url": pick["cover"]}, safe=bool(exact),
                  note=None if exact else f"Closest match is “{pick['title']}” – check it before applying.")


# ---------------------------------------------------------------- duplicates and discs

def clean_author(b):
    a = b["authors"][0] if b["authors"] else ""
    sp = split_read_by(a)
    return sp[0] if sp else a


def _author_key(b):
    a = b["authors"][0] if b["authors"] else ""
    sp = split_read_by(a)
    return fold(sp[0] if sp else a)


def _same_audio(a, b):
    da, db = disc_number(a["rel"].rsplit("/", 1)[-1]), disc_number(b["rel"].rsplit("/", 1)[-1])
    if da is not None and db is not None and da != db:
        return False
    if len(a["durs"]) != len(b["durs"]) or not a["durs"]:
        return False
    return all(abs(x - y) <= 1.5 for x, y in zip(a["durs"], b["durs"]))


def _best(books, in_progress):
    return max(books, key=lambda b: (b["id"] in in_progress, b["cover"], b["desc"], len(b["narrators"]) > 0,
                                     bool(b["series"]), not COPY_NAME.search(b["rel"]), b["size"], -b["added"]))


def check_duplicates_and_discs(books, in_progress, can_move):
    issues = []
    groups = defaultdict(list)
    for b in books:
        if not b["missing"]:
            groups[(title_key(b["title"]), _author_key(b))].append(b)
    for (tk, _ak), grp in groups.items():
        if len(grp) < 2 or not tk:
            continue
        # Copies have the same audio: same number of files, each the same length (within 1.5s).
        # Folders with different disc numbers ("Disc 1", "Disc 2") are never copies of each other.
        clusters = []
        for b in sorted(grp, key=lambda b: natural_key(b["rel"])):
            for c in clusters:
                if _same_audio(c[0], b):
                    c.append(b)
                    break
            else:
                clusters.append([b])
        dupes = [c for c in clusters if len(c) > 1]
        for c in dupes:
            keep = _best(c, in_progress)
            others = [b for b in c if b is not keep]
            plan = [f"Keep: {keep['rel']}"] + [f"Move to trash: {b['rel']}" for b in others]
            fix = {"type": "dedupe", "library": keep["library"], "keep": keep["id"], "remove": [b["id"] for b in others]}
            note = None
            if any(b["id"] in in_progress for b in others):
                note = "A copy being removed has listening progress; it won't carry over."
            if not can_move:
                fix, note = None, "Mount the library and a trash folder to fix this from here (see README)."
            issues.append(_issue("duplicate", c, f"{keep['title']} – {len(c)} copies", plan=plan, fix=fix, note=note))
        if dupes:
            continue  # sort the copies out first; disc merging shows up on the next scan
        nums = [disc_number(b["rel"].rsplit("/", 1)[-1], b["title"]) for b in grp]
        has_markers = all(n is not None for n in nums) and len(set(nums)) == len(nums)
        order = sorted(grp, key=lambda b: (disc_number(b["rel"].rsplit("/", 1)[-1], b["title"]) or 0,
                                           natural_key(b["rel"]))) if has_markers else \
            sorted(grp, key=lambda b: natural_key(b["rel"]))
        title = strip_unabridged(order[0]["title"])
        author = clean_author(order[0])
        plan = [f"Disc {i + 1}: {b['rel']} ({len(b['files'])} file{'s' if len(b['files']) != 1 else ''})"
                for i, b in enumerate(order)]
        plan.append(f"Into one folder: {safe_name(author) + '/' if author else ''}{safe_name(title)}")
        note = None if has_markers else "No disc numbers in the folder names, so the order is a guess – check it."
        fix = {"type": "merge", "library": order[0]["library"], "order": [b["id"] for b in order], "title": title,
               "author": author}
        if not can_move:
            fix, note = None, "Mount the library and a trash folder to fix this from here (see README)."
        elif any(b["is_file"] or not b["files"] for b in order):
            fix, note = None, "Some parts are single files sitting loose in a folder; merge them by hand."
        issues.append(_issue("multi_disc", order, f"{title} – {len(order)} parts", plan=plan, fix=fix, note=note))
    return issues


def check_volume_sets(books):
    """Numbered volumes ('Lessons 1-5', 'Lessons 6-10'…) by the same author → one series."""
    issues, handled = [], set()
    groups = defaultdict(list)
    for b in books:
        t = strip_unabridged(b["title"])
        t = re.split(r"\s*:\s+", t, maxsplit=1)[0]
        m = RANGE.search(t)
        if not m:
            continue
        base = (t[:m.start()] + t[m.end():]).strip(" -–—,")
        groups[(fold(base), _author_key(b), fold(m.group(1)).rstrip("s"))].append((int(m.group(2)), base, b))
    for (_k, _a, _w), grp in groups.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda x: x[0])
        bs = [b for _, _, b in grp]
        current = {series_key(n) for b in bs for n, s in b["series"] if s and n not in bogus_series(b)}
        if len(current) == 1 and all(any(s for _, s in b["series"]) for b in bs):
            continue  # already a proper numbered series
        author = bs[0]["authors"][0] if bs[0]["authors"] else ""
        base = grp[0][1]
        name = base if fold(author) in fold(base) or not author else f"{author} {base}"
        ops, changes = [], []
        for i, b in enumerate(bs, 1):
            keep = [s for s in b["series"] if s[0] not in bogus_series(b) and series_key(s[0]) != series_key(name)]
            new = keep + [[name, str(i)]]
            ops.append(_meta_fix(b, {"series": new}))
            changes.append({"field": strip_unabridged(b["title"]).split(":")[0], "before": _series_txt(b["series"]),
                            "after": _series_txt(new)})
            handled.add(b["id"])
        issues.append(_issue("volume_set", bs, f"{name} – {len(bs)} volumes", changes,
                             {"type": "metadata_many", "ops": ops}))
    return issues, handled


# ---------------------------------------------------------------- series gaps

def series_report(books, lookups):
    """Per series: what you have, what Audible lists, what's missing. Also number fixes."""
    by_series = defaultdict(list)
    names = defaultdict(set)
    for b in books:
        if b["missing"]:
            continue
        for n, s in b["series"]:
            if n in bogus_series(b):
                continue
            by_series[series_key(n)].append((b, n, s))
            names[series_key(n)].add(n)
    report, issues, errors = [], [], []
    for key, entries in sorted(by_series.items()):
        display = sorted(names[key], key=len)[0]
        owned = [{"id": b["id"], "title": b["title"], "sequence": clean_seq(s) or s} for b, _n, s in entries]
        row = {"name": display, "owned": sorted(owned, key=lambda o: seq_sort(o["sequence"])),
               "missing": [], "total": None, "found": False, "url": None}
        if lookups:
            try:
                s = audible.find_series(display, [(b["title"], b["authors"][0] if b["authors"] else "")
                                                  for b, _n, _s in entries])
                if s:
                    data = audible.series_books(s["asin"])
                    row.update(found=True, total=len(data["books"]), url=audible.web_url(s["asin"]),
                               audible_name=data["name"] or s["title"])
                    by_title = {title_key(x["title"]): x for x in data["books"] if x["title"]}
                    have = set()
                    for b, n, seq in entries:
                        hit = by_title.get(title_key(b["title"]))
                        if not hit:
                            continue
                        have.add(hit["sequence"])
                        mine = clean_seq(seq)
                        if not mine:
                            new = [[nn, hit["sequence"] if nn == n else ss] for nn, ss in b["series"]]
                            issues.append(_issue("series_number_missing", [b], f"{b['title']}: {n} #{hit['sequence']}",
                                                 [{"field": "Series", "before": _series_txt(b["series"]),
                                                   "after": _series_txt(new)}], _meta_fix(b, {"series": new})))
                        elif mine != hit["sequence"]:
                            new = [[nn, hit["sequence"] if nn == n else ss] for nn, ss in b["series"]]
                            issues.append(_issue("series_number_mismatch", [b],
                                                 f"{b['title']}: #{mine} here, #{hit['sequence']} on Audible",
                                                 [{"field": "Series", "before": _series_txt(b["series"]),
                                                   "after": _series_txt(new)}], _meta_fix(b, {"series": new})))
                    owned_seqs = {clean_seq(s) for _b, _n, s in entries if clean_seq(s)} | have
                    row["missing"] = [x for x in data["books"] if x["sequence"] not in owned_seqs]
                    row["have"] = len(owned_seqs & {x["sequence"] for x in data["books"]})
            except Exception as e:
                log.info("Series lookup failed for %s: %s", display, e)
                errors.append(f"{display}: {e}")
        report.append(row)
    return report, issues, errors


# ---------------------------------------------------------------- run everything

def run(items, abs_api=None, in_progress=(), can_move=False, lookups=True):
    books = [view(it) for it in items if it.get("mediaType", "book") == "book"]
    issues = []
    names_by_key = defaultdict(set)
    for b in books:
        for n, _s in b["series"]:
            names_by_key[series_key(n)].add(n)

    vol_issues, in_volume_set = check_volume_sets(books)
    for b in books:
        if b["missing"]:
            continue
        for fn in (check_unabridged, check_title_subtitle, check_narrator_in_author, check_author_credential):
            i = fn(b)
            if i:
                issues.append(i)
        i = check_series_suffix(b, names_by_key)
        if i:
            issues.append(i)
        if b["id"] not in in_volume_set:
            i = check_series_is_subtitle(b, lookups)
            if i:
                issues.append(i)
        if abs_api is not None:
            i = check_missing_cover(b, abs_api, lookups)
            if i:
                issues.append(i)
    issues += vol_issues
    issues += check_duplicates_and_discs(books, set(in_progress), can_move)
    report, series_issues, errors = series_report(books, lookups)
    issues += series_issues
    return {"issues": issues, "series": report, "errors": errors, "books": len(books)}
