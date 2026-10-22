"""Small client for the parts of the Audiobookshelf API the doctor uses."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class ABSError(Exception):
    pass


def _explain(e, method, path, url):
    """Turn an HTTP error into something that says what to change."""
    body = e.read().decode("utf-8", "replace")
    headers = {k.lower(): v for k, v in (e.headers or {}).items()}
    from_proxy = ("cf-ray" in headers or "cloudflare" in headers.get("server", "").lower()
                  or body.lstrip().lower().startswith(("<!doctype", "<html")))
    if from_proxy and e.code in (401, 403, 429, 503):
        who = "Cloudflare" if "cf-ray" in headers or "cloudflare" in headers.get("server", "").lower() \
            else "A proxy in front of Audiobookshelf"
        return (f"{who} blocked the request to {url} ({e.code}), so it never reached Audiobookshelf. "
                f"Set ABS_URL to the server's local address instead, e.g. http://192.168.1.10:13378.")
    if e.code == 401:
        return ("Audiobookshelf didn't accept the API key (401). Check ABS_TOKEN is the whole key, "
                "and that the key is switched on under Settings > API Keys.")
    if e.code == 403:
        if "/libraries/" in path or path.endswith("/libraries"):
            return ("Audiobookshelf says the API key's user can't use this library (403). "
                    "Make the key for an admin user, or give that user access to the library.")
        if method != "GET":
            return (f"Audiobookshelf won't let the API key's user make changes ({method} {path}, 403). "
                    "The key's user needs permission to update and upload (an admin user is easiest).")
        return f"Audiobookshelf refused {method} {path} (403): {body[:150]}"
    return f"{method} {path} failed: {e.code} {body[:200]}"


class ABS:
    def __init__(self, url=None, token=None):
        self.url = (url or config.ABS_URL).rstrip("/")
        self.token = token if token is not None else config.ABS_TOKEN

    def _req(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.url + path, data=data, method=method)
        r.add_header("Authorization", "Bearer " + self.token)
        r.add_header("Accept", "application/json")
        # Cloudflare and some proxies block Python's default user agent outright.
        r.add_header("User-Agent", "abs-library-doctor/1.0 (+https://github.com/ssrt33/abs-library-doctor)")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ABSError(_explain(e, method, path, self.url)) from e
        except urllib.error.URLError as e:
            raise ABSError(f"Can't reach Audiobookshelf at {self.url}: {e.reason}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    # ---- reading ----
    def status(self):
        return self._req("GET", "/status")

    def me(self):
        return self._req("GET", "/api/me")

    def libraries(self):
        return self._req("GET", "/api/libraries")["libraries"]

    def item_ids(self, library_id):
        res = self._req("GET", f"/api/libraries/{library_id}/items?limit=0&minified=1", timeout=120)
        return [it["id"] for it in res.get("results", [])]

    def items_full(self, ids, chunk=50):
        """Full items: structured authors/series and audio files."""
        out = []
        for i in range(0, len(ids), chunk):
            res = self._req("POST", "/api/items/batch/get", {"libraryItemIds": ids[i:i + chunk]}, timeout=120)
            out += res.get("libraryItems", [])
        return out

    def item(self, item_id):
        return self._req("GET", f"/api/items/{item_id}?expanded=1")

    def search_books(self, title, author="", provider=None):
        q = urllib.parse.urlencode({"provider": provider or config.ABS_PROVIDER, "title": title, "author": author})
        res = self._req("GET", f"/api/search/books?{q}", timeout=60)
        return res if isinstance(res, list) else []

    # ---- writing ----
    def update_metadata(self, item_id, metadata):
        return self._req("PATCH", f"/api/items/{item_id}/media", {"metadata": metadata})

    def set_cover_url(self, item_id, url):
        return self._req("POST", f"/api/items/{item_id}/cover", {"url": url}, timeout=60)

    def remove_cover(self, item_id):
        return self._req("DELETE", f"/api/items/{item_id}/cover")

    def set_track_order(self, item_id, inos):
        return self._req("PATCH", f"/api/items/{item_id}/tracks", {"orderedFileData": [{"ino": i} for i in inos]})

    def forget_item(self, item_id):
        """Remove an item from the database only. Files are never touched (no hard delete)."""
        return self._req("DELETE", f"/api/items/{item_id}")

    def scan_library(self, library_id):
        return self._req("POST", f"/api/libraries/{library_id}/scan", {})

    def wait_for(self, check, timeout=180, every=2):
        """Poll check() until it returns something truthy."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = check()
                if v:
                    return v
            except ABSError:
                pass
            time.sleep(every)
        return None
