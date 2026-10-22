"""A tiny stand-in for the Audible catalog API, for tests. Run: python tests/mock_audible.py 8899"""
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

RAPP = ["American Assassin", "Kill Shot", "Transfer of Power", "The Third Option", "Separation of Power",
        "Executive Power", "Memorial Day", "Consent to Kill", "Act of Treason", "Protect and Defend",
        "Extreme Measures", "Pursuit of Honor", "The Last Man", "The Survivor", "Order to Kill",
        "Enemy of the State", "Red War", "Lethal Agent", "Total Power"]
SERIES = {
    "SRAPP": {"title": "Mitch Rapp", "author": "Vince Flynn",
              "books": [(str(i + 1), f"RAPP{i + 1:02d}", t) for i, t in enumerate(RAPP)]},
    "SLEGION": {"title": "Legion", "author": "Brandon Sanderson",
                "books": [("1", "LEG01", "Legion"), ("2", "LEG02", "Skin Deep"), ("3", "LEG03", "Lies of the Beholder")]},
}
PRODUCTS = {}
for sasin, s in SERIES.items():
    for seq, asin, title in s["books"]:
        PRODUCTS[asin] = {"asin": asin, "title": title, "release_date": "2010-01-01",
                          "authors": [{"name": s["author"]}],
                          "series": [{"asin": sasin, "title": s["title"], "sequence": seq}]}
    # Audible also lists box sets with range sequences; the doctor should skip them.
    s["books"].append(("1-3", f"{sasin}BOX", f"{s['title']} Box Set"))
    PRODUCTS[f"{sasin}BOX"] = {"asin": f"{sasin}BOX", "title": f"{s['title']} Box Set", "authors": [{"name": s["author"]}]}


def fold(s):
    return "".join(c for c in s.lower() if c.isalnum() or c == " ")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/1.0/catalog/products":
            if "asins" in q:
                return self._send({"products": [PRODUCTS[a] for a in q["asins"].split(",") if a in PRODUCTS]})
            kw = fold(q.get("keywords", ""))
            hits = [p for p in PRODUCTS.values() if fold(p["title"]) in kw and "Box" not in p["title"]]
            hits.sort(key=lambda p: -len(p["title"]))
            return self._send({"products": hits})
        if u.path.startswith("/1.0/catalog/products/"):
            asin = u.path.rsplit("/", 1)[-1]
            if asin in SERIES:
                s = SERIES[asin]
                rels = [{"asin": a, "sequence": seq, "sort": str(i), "relationship_to_product": "child",
                         "relationship_type": "series"} for i, (seq, a, _t) in enumerate(s["books"])]
                return self._send({"product": {"asin": asin, "title": s["title"], "relationships": rels}})
            if asin in PRODUCTS:
                return self._send({"product": PRODUCTS[asin]})
            return self._send({"error": "not found"}, 404)
        self._send({"error": "not found"}, 404)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1] if len(sys.argv) > 1 else 8899)), H).serve_forever()
