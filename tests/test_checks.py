"""Checks against hand-built items shaped like Audiobookshelf's. No network needed: python -m pytest"""
import itertools

from doctor import checks
from doctor.textutil import clean_seq, disc_number, split_read_by, strip_unabridged, title_key

_n = itertools.count(1)


def item(title, authors, rel, subtitle="", series=(), narrators=(), durs=(100.0,), cover=False, lib="L1"):
    i = next(_n)
    return {
        "id": f"item{i}", "libraryId": lib, "path": f"/audiobooks/{rel}", "relPath": rel, "isFile": False,
        "isMissing": False, "mediaType": "book", "addedAt": i,
        "media": {
            "coverPath": "/x/cover.jpg" if cover else None, "duration": sum(durs), "size": int(sum(durs) * 1000),
            "metadata": {"title": title, "subtitle": subtitle, "authors": [{"id": a, "name": a} for a in authors],
                         "narrators": list(narrators),
                         "series": [{"id": n, "name": n, "sequence": s} for n, s in series]},
            "audioFiles": [{"ino": f"{i}-{k}", "index": k, "duration": d,
                            "metadata": {"filename": f"track{k:02d}.mp3", "path": f"/audiobooks/{rel}/track{k:02d}.mp3"}}
                           for k, d in enumerate(durs, 1)],
        },
    }


def run(items, **kw):
    kw.setdefault("lookups", False)
    kw.setdefault("can_move", True)
    return checks.run(items, **kw)


def kinds(res):
    return sorted(i["kind"] for i in res["issues"])


# ---- text rules

def test_text_rules():
    assert strip_unabridged("Kill Shot (Unabridged)") == "Kill Shot"
    assert strip_unabridged("Red War [Unabridged Edition]") == "Red War"
    assert strip_unabridged("Abridged Thoughts") == "Abridged Thoughts"
    assert split_read_by("John Grisham read by Dennis Boutsikaris") == ("John Grisham", "Dennis Boutsikaris")
    assert split_read_by("Stephen King - Narrated by Will Patton") == ("Stephen King", "Will Patton")
    assert split_read_by("Ready Player Two") is None
    assert disc_number("The Broker Disc 3") == 3 and disc_number("CD02") == 2 and disc_number("Part 4 of 9") == 4
    assert disc_number("Legion") is None
    assert title_key("The Optimal Dose: Restore Your Health (Unabridged)") == "optimal dose"
    assert clean_seq("08") == "8" and clean_seq("2.5") == "2.5" and clean_seq("1-3") == ""


# ---- metadata

def test_unabridged_and_subtitle_are_safe():
    res = run([item("Kill Shot (Unabridged)", ["Vince Flynn"], "Vince Flynn/Kill Shot"),
               item("The Optimal Dose: Restore Your Health", ["Judson Somerville"], "J/Optimal",
                    subtitle="Restore Your Health")])
    by = {i["kind"]: i for i in res["issues"]}
    assert by["unabridged"]["safe"] and by["unabridged"]["fix"]["set"] == {"title": "Kill Shot"}
    assert by["title_subtitle"]["fix"]["set"] == {"title": "The Optimal Dose"}


def test_narrator_split_and_credential():
    res = run([item("The Broker", ["John Grisham read by Dennis Boutsikaris"], "G/The Broker"),
               item("The Optimal Dose", ["Judson Somerville", "Md"], "J/Optimal")])
    by = {i["kind"]: i for i in res["issues"]}
    assert by["narrator_in_author"]["fix"]["set"] == {"authors": ["John Grisham"], "narrators": ["Dennis Boutsikaris"]}
    assert by["author_credential"]["fix"]["set"] == {"authors": ["Judson Somerville MD"]}


def test_series_suffix_merges_toward_plain_name():
    res = run([item("Memorial Day", ["Vince Flynn"], "V/MD", series=[("Mitch Rapp", "7")]),
               item("Executive Power", ["Vince Flynn"], "V/EP", series=[("Mitch Rapp Series", "")])])
    fx = [i for i in res["issues"] if i["kind"] == "series_suffix"]
    assert len(fx) == 1 and fx[0]["fix"]["set"] == {"series": [["Mitch Rapp", ""]]}


def test_series_named_after_book_is_fine_but_subtitle_series_is_not():
    res = run([item("Legion", ["Brandon Sanderson"], "B/Legion", series=[("Legion", "1")]),
               item("Kill Shot", ["Vince Flynn"], "V/KS", subtitle="An American Assassin Thriller",
                    series=[("An American Assassin Thriller", "")])])
    bad = [i for i in res["issues"] if i["kind"] == "series_is_subtitle"]
    assert [b["books"][0]["title"] for b in bad] == ["Kill Shot"]
    assert not bad[0]["safe"]


# ---- duplicates and discs

def test_discs_are_not_mistaken_for_copies():
    # real discs are often the same length; the disc numbers tell them apart
    parts = [item("The Broker", ["John Grisham"], f"G/The Broker Disc {n}", durs=(4700.0,)) for n in (2, 1, 3)]
    res = run(parts)
    assert kinds(res) == ["multi_disc"]
    merge = res["issues"][0]
    assert [b["id"] for b in merge["books"]] == [parts[1]["id"], parts[0]["id"], parts[2]["id"]]
    assert merge["fix"]["type"] == "merge" and not merge["safe"] and merge["note"] is None


def test_true_copies_found_and_best_kept():
    a = item("A Dweller on Two Planets", ["F S Oliver"], "F/A Dweller (copy)", durs=(300.0, 250.0))
    b = item("A Dweller on Two Planets", ["F S Oliver"], "F/A Dweller", durs=(300.4, 249.6), cover=True)
    res = run([a, b])
    assert kinds(res) == ["duplicate"]
    fix = res["issues"][0]["fix"]
    assert fix["keep"] == b["id"] and fix["remove"] == [a["id"]]


def test_file_fixes_off_without_mounts():
    res = run([item("The Broker", ["John Grisham"], f"G/The Broker Disc {n}", durs=(60.0 + n,)) for n in (1, 2)],
              can_move=False)
    assert res["issues"][0]["fix"] is None and "Mount" in res["issues"][0]["note"]


def test_volume_set_becomes_series():
    sub = "Learn to Speak and Understand Spanish with Pimsleur Language Programs"
    vols = [item(f"Spanish Level 1 Lessons {r}: {sub}", ["Pimsleur"], f"P/{r}", subtitle=sub, series=[(sub, "")])
            for r in ("6-10", "1-5", "11-15")]
    res = run(vols)
    vs = [i for i in res["issues"] if i["kind"] == "volume_set"][0]
    assert vs["summary"].startswith("Pimsleur Spanish Level 1")
    seqs = {op["item"]: op["set"]["series"] for op in vs["fix"]["ops"]}
    assert seqs[vols[1]["id"]] == [["Pimsleur Spanish Level 1", "1"]]
    assert seqs[vols[0]["id"]] == [["Pimsleur Spanish Level 1", "2"]]
    # the bogus subtitle series is handled by the volume set, not reported twice
    assert not [i for i in res["issues"] if i["kind"] == "series_is_subtitle"]


# ---- covers

class FakeABS:
    def __init__(self, results):
        self.results = results

    def search_books(self, title, author="", provider=None):
        return self.results


def test_cover_exact_match_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("doctor.config.CONFIG_DIR", str(tmp_path))
    fake = FakeABS([{"title": "Term Limits", "author": "Vince Flynn", "cover": "https://img/x.jpg"}])
    res = checks.run([item("Term Limits", ["Vince Flynn"], "V/Term Limits")], fake, lookups=True)
    cov = [i for i in res["issues"] if i["kind"] == "missing_cover"][0]
    assert cov["safe"] and cov["fix"] == {"type": "cover", "item": cov["books"][0]["id"], "url": "https://img/x.jpg"}


def test_cover_loose_match_waits_for_you(tmp_path, monkeypatch):
    monkeypatch.setattr("doctor.config.CONFIG_DIR", str(tmp_path))
    fake = FakeABS([{"title": "Term Limits: A Novel of Suspense?", "author": "Someone Else", "cover": "https://img/y.jpg"}])
    res = checks.run([item("Term Limits", ["Vince Flynn"], "V/Term Limits")], fake, lookups=True)
    cov = [i for i in res["issues"] if i["kind"] == "missing_cover"][0]
    assert not cov["safe"] and cov["note"]


def test_issue_ids_are_stable():
    mk = lambda: [item("Kill Shot (Unabridged)", ["Vince Flynn"], "V/KS")]
    a, b = run(mk()), run(mk())
    assert a["issues"][0]["id"] != b["issues"][0]["id"]  # different items → different ids
    x = mk()
    assert run(x)["issues"][0]["id"] == run(x)["issues"][0]["id"]


# ---- overlapping library folders (the same files seen twice)

def twin(it, rel):
    """Same folder on disk, seen again through an outer library folder."""
    import copy
    t = copy.deepcopy(it)
    t["id"] = it["id"] + "-twin"
    t["relPath"] = rel
    return t


def test_same_folder_twice_is_not_a_duplicate():
    a = item("Not Till We Are Lost", ["Dennis E. Taylor"], "Dennis E. Taylor/Not Till We Are Lost")
    a["path"] = "/Media/audiobooks/Dennis E. Taylor/Not Till We Are Lost"
    a["ino"] = "555"
    b = twin(a, "audiobooks/Dennis E. Taylor/Not Till We Are Lost")
    res = run([a, b])
    assert kinds(res) == ["double_scanned"]
    assert res["issues"][0]["fix"] is None


def test_twins_of_discs_still_offer_one_merge():
    discs = []
    for n in (1, 2):
        d = item("The Broker", ["John Grisham"], f"G/The Broker Disc {n}", durs=(60.0 + n,))
        d["path"] = f"/Media/audiobooks/G/The Broker Disc {n}"
        d["ino"] = f"9{n}"
        discs += [d, twin(d, f"audiobooks/G/The Broker Disc {n}")]
    res = run(discs)
    assert kinds(res) == ["double_scanned", "double_scanned", "multi_disc"]  # one notice per folder
    assert len(next(i for i in res["issues"] if i["kind"] == "multi_disc")["books"]) == 2


def test_folder_overlaps():
    libs = [{"name": "audiobooks", "folders": [{"fullPath": "/Media"}, {"fullPath": "/Media/audiobooks"}]}]
    assert checks.folder_overlaps(libs) == [{"outer": "/Media", "inner": "/Media/audiobooks",
                                             "outer_lib": "audiobooks", "inner_lib": "audiobooks"}]
    assert checks.folder_overlaps([{"name": "x", "folders": [{"fullPath": "/a"}, {"fullPath": "/ab"}]}]) == []


def test_dedupe_refuses_same_files(tmp_path, monkeypatch):
    import pytest
    from doctor import fixes
    monkeypatch.setattr("doctor.config.PATH_MAP", "/Media=" + str(tmp_path))
    (tmp_path / "audiobooks" / "Book").mkdir(parents=True)
    a = {"path": "/Media/audiobooks/Book", "rel": "Book"}
    b = {"path": "/Media/audiobooks/Book/", "rel": "audiobooks/Book"}
    with pytest.raises(fixes.FixError):
        fixes._same_files_guard([a, b])
    c = {"path": "/Media/audiobooks", "rel": "audiobooks"}
    with pytest.raises(fixes.FixError):
        fixes._same_files_guard([c, a])


def test_volume_set_fills_gap_in_existing_series():
    vols = []
    for n, r in enumerate(("1-5", "6-10", "11-15", "16-20", "21-25", "26-30"), 1):
        series = [] if n == 5 else [("Pimsleur Spanish", str(n))]
        vols.append(item(f"Spanish Level 1 Lessons {r}", ["Pimsleur"], f"P/{r}", series=series))
    res = run(vols)
    vs = [i for i in res["issues"] if i["kind"] == "volume_set"]
    assert len(vs) == 1 and vs[0]["summary"] == "Pimsleur Spanish – 1 missing number"
    ops = vs[0]["fix"]["ops"]
    assert len(ops) == 1 and ops[0]["item"] == vols[4]["id"] and ops[0]["set"] == {"series": [["Pimsleur Spanish", "5"]]}


def test_volume_set_complete_series_is_left_alone():
    vols = [item(f"Spanish Level 1 Lessons {r}", ["Pimsleur"], f"P/{r}", series=[("Pimsleur Spanish", str(n))])
            for n, r in enumerate(("1-5", "6-10"), 1)]
    assert not [i for i in run(vols)["issues"] if i["kind"] == "volume_set"]


def test_copy_outside_staging_folder_is_kept():
    a = item("The Broker", ["John Grisham"], "audiobooks/untagged/The Broker (2006)", durs=(300.0,), cover=True)
    b = item("The Broker", ["John Grisham"], "audiobooks/The Broker (2006)", durs=(300.2,))
    dup = run([a, b])["issues"][0]
    assert dup["kind"] == "duplicate" and dup["fix"]["keep"] == b["id"]
    assert [c["id"] for c in dup["copies"]] == [b["id"], a["id"]]


# ---- the series list stays put between checks

def test_series_survive_one_incomplete_check():
    from doctor.engine import merge_series
    rapp = {"key": "mitch rapp", "name": "Mitch Rapp", "owned": [1], "missing": [{"sequence": "9"}], "found": True, "total": 25}
    after_one = merge_series([rapp], [])            # check caught ABS mid-rescan: no series seen
    assert after_one[0]["name"] == "Mitch Rapp" and after_one[0]["missing"] == rapp["missing"] and after_one[0]["stale"]
    assert merge_series(after_one, []) == []         # gone two checks running: really gone
    back = merge_series(after_one, [dict(rapp)])     # seen again: back to normal
    assert "stale" not in back[0] and "absent" not in back[0]


def test_series_keep_last_list_when_audible_fails():
    from doctor.engine import merge_series
    good = {"key": "legion", "name": "Legion", "owned": [1], "missing": [{"sequence": "2"}], "found": True, "total": 3}
    failed = {"key": "legion", "name": "Legion", "owned": [1, 2], "missing": [], "found": False, "lookup_failed": True}
    r = merge_series([good], [failed])[0]
    assert r["found"] and r["missing"] == good["missing"] and r["owned"] == [1, 2] and r["stale"]


def test_series_spelled_differently_are_one_series():
    from doctor.engine import merge_series, dedupe_series
    old = [{"key": "the enemy", "name": "The Enemy", "owned": [1], "missing": [], "found": True},
           {"name": "A Jack Ryan Jr. Novel", "owned": [1], "missing": [], "found": True}]  # saved by an older version
    new = [{"key": "enemy", "name": "The Enemy", "owned": [1], "missing": [], "found": True},
           {"key": "a jack ryan jr novel", "name": "A Jack Ryan Jr. Novel", "owned": [1], "missing": [], "found": True}]
    out = merge_series(old, new)
    assert [r["name"] for r in out] == ["A Jack Ryan Jr. Novel", "The Enemy"] and not any(r.get("stale") for r in out)
    doubled = [dict(new[0]), {**old[0], "stale": True, "absent": 1}]
    assert [r.get("stale") for r in dedupe_series(doubled)] == [None]
