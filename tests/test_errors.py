"""The connection error messages point at the right fix."""
import io
import urllib.error
from email.message import Message

from doctor.abs_client import _explain


def err(code, body="", **headers):
    h = Message()
    for k, v in headers.items():
        h[k.replace("_", "-")] = v
    return urllib.error.HTTPError("http://x", code, "x", h, io.BytesIO(body.encode()))


def test_cloudflare_block():
    m = _explain(err(403, "<!DOCTYPE html><title>Attention Required</title>", cf_ray="abc"), "GET", "/status", "https://a.b")
    assert m.startswith("Cloudflare blocked") and "local address" in m


def test_other_proxy_block():
    m = _explain(err(403, "<html>Forbidden</html>", server="nginx"), "GET", "/api/libraries", "https://a.b")
    assert m.startswith("A proxy in front of Audiobookshelf")


def test_bad_key():
    assert "didn't accept the API key" in _explain(err(401, "Unauthorized"), "GET", "/api/libraries", "u")


def test_no_library_access():
    assert "can't use this library" in _explain(err(403, "Forbidden"), "GET", "/api/libraries/123/items", "u")


def test_no_write_permission():
    assert "won't let the API key's user make changes" in _explain(err(403, "Forbidden"), "PATCH", "/api/items/1/media", "u")
