"""Series lists come back with book names (Audible drops titles unless product_desc is asked for)."""
import threading
from http.server import HTTPServer

from doctor import audible
from tests.mock_audible import H


def test_series_books_have_titles(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr("doctor.config.AUDIBLE_API", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr("doctor.config.CONFIG_DIR", str(tmp_path))
    try:
        s = audible.find_series("Mitch Rapp", [("Consent to Kill", "Vince Flynn")])
        books = audible.series_books(s["asin"])["books"]
        assert books[8] == {**books[8], "sequence": "9", "title": "Act of Treason"}
        assert all(b["title"] for b in books)
        assert "1-3" not in [b["sequence"] for b in books]  # box sets skipped
    finally:
        srv.shutdown()
