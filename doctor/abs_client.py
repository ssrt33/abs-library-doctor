"""Small client for the parts of the Audiobookshelf API the doctor uses."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class ABSError(Exception):
    pass


class ABS:
    def __init__(self, url=None, token=None):
        self.url = (url or config.ABS_URL).rstrip("/")
        self.token = token if token is not None else config.ABS_TOKEN

    def _req(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.url + path, data=data, method=method)
        r.add_header("Authorization", "Bearer " + self.token)
        r.add_header("Accept", "application/json")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (401, 403):
                raise ABSError(f"Audiobookshelf refused the API key ({e.code}). Check ABS_TOKEN.") from e
            raise ABSError(f"{method} {path} failed: {e.code} {detail}") from e
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
