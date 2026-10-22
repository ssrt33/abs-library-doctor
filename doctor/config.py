"""Settings, all overridable with environment variables."""
import os


def _bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Your Audiobookshelf server, as the doctor container can reach it. Include the sub-path if you
# use one, e.g. http://192.168.1.10:13378 or https://audiobooks.example.com/audiobookshelf
ABS_URL = os.environ.get("ABS_URL", "http://audiobookshelf:13378").rstrip("/")
# An API key from Audiobookshelf (Settings > API Keys) for a user that can update and upload.
ABS_TOKEN = os.environ.get("ABS_TOKEN", "").strip()
# Only look at these libraries (names or ids, comma separated). Empty = every book library.
LIBRARIES = [x.strip() for x in os.environ.get("LIBRARIES", "").split(",") if x.strip()]

# How Audiobookshelf's paths map to paths inside this container, as "abs_path=local_path".
# Needed only for fixes that move files (merging discs, putting duplicates aside). If both
# containers mount the library at /audiobooks, the default works as is.
PATH_MAP = os.environ.get("PATH_MAP", "/audiobooks=/audiobooks")
# Where duplicate copies and leftover files are moved instead of being deleted. Mount a folder
# here that is OUTSIDE the library so Audiobookshelf doesn't scan it.
TRASH_DIR = os.environ.get("TRASH_DIR", "/trash")
# Where the app keeps its own state (issues, history, settings, lookup cache).
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

PORT = int(os.environ.get("PORT", "8687"))

# Starting values for settings you can also change on the page.
AUTO_FIX = _bool("AUTO_FIX", True)
SCAN_EVERY_HOURS = float(os.environ.get("SCAN_EVERY_HOURS", "6"))

# Audible catalog, used for series gaps. Region: us, uk, ca, au, de, fr, it, es, in, jp
AUDIBLE_REGION = os.environ.get("AUDIBLE_REGION", "us")
AUDIBLE_API = os.environ.get("AUDIBLE_API", "").rstrip("/")  # override for testing
# Provider Audiobookshelf uses for cover and book lookups (see its Match tab for the list).
ABS_PROVIDER = os.environ.get("ABS_PROVIDER", "audible")

# Optional push notifications (plain-text POST, e.g. an ntfy topic URL).
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").strip()

# Ownership for files the app moves or creates (TrueNAS "apps" user is 568).
PUID = int(os.environ.get("PUID", "568"))
PGID = int(os.environ.get("PGID", "568"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
