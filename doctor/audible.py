"""Audible catalog lookups for series lists (which books a series has, in order).

Results are cached on disk for a week so a library scan doesn't hammer Audible.
"""
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

from . import config
from .textutil import clean_seq, fold, seq_sort, series_key, title_key

log = logging.getLogger("doctor")
UA = "Mozilla/5.0 (abs-library-doctor)"
TLD = {"us": "com", "uk": "co.uk", "ca": "ca", "au": "com.au", "de": "de", "fr": "fr",
       "it": "it", "es": "es", "in": "in", "jp": "co.jp"}
WEEK = 7 * 24 * 3600
_lock = threading.Lock()


def _tld():
    return TLD.get(config.AUDIBLE_REGION, "com")


def api_base():
    return config.AUDIBLE_API or f"https://api.audible.{_tld()}"


def web_url(asin):
    return f"https://www.audible.{_tld()}/pd/{asin}"


def _cache_path():
    return os.path.join(config.CONFIG_DIR, "audible-cache.json")


def _load_cache():
    try:
        with open(_cache_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cached(key, fn, ttl=WEEK):
    with _lock:
        cache = _load_cache()
        hit = cache.get(key)
        if hit and time.time() - hit["t"] < ttl:
            return hit["v"]
    v = fn()
    with _lock:
        cache = _load_cache()
        cache[key] = {"t": time.time(), "v": v}
        # keep the cache from growing forever
        if len(cache) > 3000:
            for k in sorted(cache, key=lambda k: cache[k]["t"])[:1000]:
                cache.pop(k)
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _cache_path())
    return v


def _get(path, params):
    url = f"{api_base()}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def search(title, author):
    """Products matching a title/author, each with its series list."""
    def fetch():
        data = _get("/1.0/catalog/products", {
            "keywords": f"{title} {author}".strip(), "num_results": 10, "products_sort_by": "Relevance",
            "response_groups": "contributors,product_attrs,series"})
        out = []
        for p in data.get("products", []):
            out.append({
                "asin": p.get("asin"),
                "title": p.get("title", ""),
                "authors": [a.get("name", "") for a in p.get("authors") or []],
                "series": [{"asin": s.get("asin"), "title": s.get("title", ""), "sequence": clean_seq(s.get("sequence"))}
                           for s in p.get("series") or [] if s.get("asin")],
            })
        return out
    return _cached(f"search:{config.AUDIBLE_REGION}:{fold(title)}|{fold(author)}", fetch)


def _products(asins):
    """Title/year/series for a list of ASINs."""
    out = {}
    for i in range(0, len(asins), 40):
        chunk = asins[i:i + 40]
        try:
            data = _get("/1.0/catalog/products", {"asins": ",".join(chunk),
                                                 "response_groups": "contributors,product_attrs,series"})
            prods = data.get("products", [])
        except Exception:
            prods = []
        got = {p.get("asin") for p in prods}
        # Fallback: one at a time for anything the batch call didn't return.
        for a in chunk:
            if a not in got:
                try:
                    prods.append(_get(f"/1.0/catalog/products/{a}",
                                      {"response_groups": "contributors,product_attrs,series"}).get("product", {}))
                except Exception as e:
                    log.debug("Audible product %s failed: %s", a, e)
        for p in prods:
            if p.get("asin"):
                out[p["asin"]] = {
                    "title": p.get("title", ""),
                    "year": (p.get("release_date") or "")[:4],
                    "authors": [x.get("name", "") for x in p.get("authors") or []],
                }
    return out


def series_books(series_asin):
    """Books in a series, one per sequence number, in order."""
    def fetch():
        data = _get(f"/1.0/catalog/products/{series_asin}", {"response_groups": "relationships,product_attrs"})
        prod = data.get("product", {})
        rels = [r for r in prod.get("relationships") or []
                if r.get("relationship_to_product") == "child" and r.get("relationship_type", "series") == "series"]
        by_seq = {}
        for r in sorted(rels, key=lambda r: seq_sort(r.get("sort") or r.get("sequence"))):
            seq = clean_seq(r.get("sequence"))
            if seq and seq not in by_seq:
                by_seq[seq] = r.get("asin")
        info = _products(list(by_seq.values()))
        books = []
        for seq, asin in sorted(by_seq.items(), key=lambda kv: seq_sort(kv[0])):
            i = info.get(asin, {})
            books.append({"sequence": seq, "asin": asin, "title": i.get("title", ""), "year": i.get("year", ""),
                          "url": web_url(asin)})
        return {"name": prod.get("title", ""), "asin": series_asin, "books": books}
    return _cached(f"series:{config.AUDIBLE_REGION}:{series_asin}", fetch)


def find_series(series_name, sample_books):
    """Find a library series on Audible using a few of its books (title, author)."""
    want = series_key(series_name)
    for title, author in sample_books[:3]:
        for p in search(title_key(title) or title, author):
            for s in p["series"]:
                if series_key(s["title"]) == want or want in series_key(s["title"]) or series_key(s["title"]) in want:
                    return s
    return None


def find_book(title, author):
    """Best Audible match for a book: same title, same author."""
    tk = title_key(title)
    ak = fold(author)
    for p in search(tk or title, author):
        if title_key(p["title"]) == tk and (not ak or any(fold(a) == ak or ak in fold(a) for a in p["authors"])):
            return p
    return None
