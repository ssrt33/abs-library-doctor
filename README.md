# Library Doctor for Audiobookshelf

Finds and fixes the messy parts of an [Audiobookshelf](https://www.audiobookshelf.org/) library:
books split into one item per disc, duplicate copies, narrators stuck in the author field,
"(Unabridged)" in titles, series named after the subtitle, missing series numbers and covers.
It also shows which books you're missing from each series.

Safe fixes happen on their own. Anything bigger, and anything that moves files, waits for you to
press **Fix**. Every change is in the history with an **Undo** button, and nothing is ever
deleted: copies and leftover files are moved to a trash folder you control.

It's a companion to [audiobook-ripper](https://github.com/ssrt33/audiobook-ripper), which keeps
new rips clean. This tidies up everything that was already there.

## What it checks

| Check | What the fix does | Applies on its own? |
|---|---|---|
| "(Unabridged)" in titles | Removes it from the title and subtitle | Yes |
| Subtitle repeated in the title | `Title: Subtitle` becomes `Title` when the subtitle has its own field | Yes |
| Narrator stuck in the author field | `John Grisham read by Dennis Boutsikaris` → author John Grisham, narrator Dennis Boutsikaris | Yes |
| Author split at a credential | `Judson Somerville` + `Md` → `Judson Somerville MD` | Yes |
| Same series under two names | `Mitch Rapp Series` → `Mitch Rapp` when that series already exists | Yes |
| Missing series number | Fills in the number from Audible's series list, matched by title | Yes |
| Missing cover | Uses Audiobookshelf's own lookup; automatic only on an exact title + author match | Yes (exact match) |
| Subtitle used as the series | Sets the real series from Audible, or clears it | Asks |
| Series number differs from Audible | Changes your number to Audible's (reading order can differ, so it asks) | Asks |
| Volumes listed as separate books | Groups "Lessons 1-5", "Lessons 6-10"… into one numbered series | Asks |
| Duplicate copies | Keeps the best copy, moves the rest to the trash folder | Asks (moves files) |
| One book split into discs | Moves every disc's audio into one `Author/Title` folder in disc order, so it's one book | Asks (moves files) |

You can choose which checks apply on their own, or turn automatic fixing off, on the page.

**How copies are told apart from discs:** copies have the same audio (same number of files, each
the same length within a second and a half). Folders with different disc numbers
(`Disc 1`, `CD 2`, `Part 3`) are never treated as copies, even when the discs are the same length.

**Series gaps:** for every series you own, the page shows which numbers you have and links to the
ones you don't, using Audible's catalog (cached for a week).

## Safety

- Metadata fixes check the book still looks the way it did at the scan before changing anything.
- Undo won't overwrite a change you made by hand afterwards unless you confirm it.
- Merges and duplicate removal only move files. If anything goes wrong partway, the moves are put
  back. Audiobookshelf items whose folders were moved are removed from its database **without**
  its delete-files option.
- After you undo a fix, that issue is ignored so automatic fixing doesn't redo it.
- Listening progress is kept on merges (the first disc's item becomes the whole book). If a copy
  being removed has progress, the page warns you.

## Install on TrueNAS (Dockge)

### 1. Make an API key in Audiobookshelf

In Audiobookshelf go to **Settings → API Keys → Add API Key**. Pick a user that can update and
upload (your admin user is fine), no expiry, and copy the key.

### 2. Make a trash folder

Create a dataset or folder **outside** the audiobook library, e.g.
`/mnt/tank/apps/abs-library-doctor/trash`, and a `config` folder next to it. Give the `apps`
user (568) read/write on both, and on the library itself.

### 3. Find the path Audiobookshelf uses

In Audiobookshelf open **Settings → Libraries**, edit your library, and look at **Folders**.
That's the path inside the Audiobookshelf container, often `/audiobooks`.

### 4. Add the stack

In Dockge, **+ Compose**, name it `abs-library-doctor`, and paste
[`docker-compose.example.yaml`](docker-compose.example.yaml). Then change:

- `ABS_URL`: how this container reaches Audiobookshelf, e.g. `http://192.168.1.10:13378`.
  If you use a sub-path, include it (`https://audiobooks.example.com/audiobookshelf`).
- `ABS_TOKEN`: the key from step 1.
- The library volume: mount the **same** host folder Audiobookshelf uses.
- `PATH_MAP`: `<path Audiobookshelf sees>=<path this container sees>`. If you mount the library
  at the same path as Audiobookshelf does (e.g. both at `/audiobooks`), leave it as is.

Deploy, then open `http://<nas-ip>:8687`. The first scan starts on its own.

Without the library and trash mounts everything still works except merging discs and removing
copies; the page tells you if they're off.

## Settings

| Variable | Default | |
|---|---|---|
| `ABS_URL` | `http://audiobookshelf:13378` | Audiobookshelf address, with sub-path if any |
| `ABS_TOKEN` | | API key (required) |
| `LIBRARIES` | all book libraries | Names or ids, comma separated. The id is in the library's address in Audiobookshelf (`…/library/<id>`); use it when two libraries share a name |
| `PATH_MAP` | `/audiobooks=/audiobooks` | `abs_path=local_path`, comma separated for several |
| `TRASH_DIR` | `/trash` | Where copies and leftovers are moved |
| `AUTO_FIX` | `true` | Starting value (change it on the page) |
| `SCAN_EVERY_HOURS` | `6` | Starting value (change it on the page) |
| `AUDIBLE_REGION` | `us` | us, uk, ca, au, de, fr, it, es, in, jp |
| `ABS_PROVIDER` | `audible` | Provider Audiobookshelf uses for cover lookups |
| `NOTIFY_URL` | | ntfy topic (or any plain-text POST URL) told when things get fixed |
| `PUID` / `PGID` | `568` | Owner for moved files |
| `PORT` | `8687` | |

## Development

```sh
pip install -r requirements.txt pytest
python -m pytest -q                       # checks, against hand-built items
ABS_URL=http://localhost:13378 ABS_TOKEN=... CONFIG_DIR=./config TRASH_DIR=./trash python -m doctor
```

`tests/mock_audible.py` is a small stand-in for the Audible catalog; point `AUDIBLE_API` at it to
test series lookups offline.

## License

MIT
