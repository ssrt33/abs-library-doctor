"""Text clean-up and matching rules shared by the checks."""
import re
import unicodedata

# "(Unabridged)", "[Unabridged]", "- Unabridged", ", Unabridged Edition"
UNABRIDGED = re.compile(r"\s*(?:[\(\[]\s*unabridged(?:\s+edition)?\s*[\)\]]|[-–—,:]\s*unabridged(?:\s+edition)?\s*$)", re.I)
# "John Grisham read by Dennis Boutsikaris", "X narrated by Y", "X - Read by Y"
READ_BY = re.compile(r"^(?P<author>.+?)\s*[-–—,;(]?\s*\b(?:read|narrated|performed)\s+by\b\s*(?P<narr>.+?)\)?\s*$", re.I)
# Bits that end up as their own "author" when a name like "Judson Somerville, MD" is split on the comma.
CREDENTIALS = {"md", "m.d.", "phd", "ph.d.", "do", "d.o.", "dds", "rn", "np", "pa", "esq", "jr", "jr.", "sr",
               "sr.", "ii", "iii", "iv", "mba", "msw", "lcsw", "dvm", "psyd", "edd", "ed.d."}
CRED_CASE = {"md": "MD", "phd": "PhD", "do": "DO", "dds": "DDS", "rn": "RN", "np": "NP", "pa": "PA", "esq": "Esq.",
             "jr": "Jr.", "sr": "Sr.", "ii": "II", "iii": "III", "iv": "IV", "mba": "MBA", "msw": "MSW",
             "lcsw": "LCSW", "dvm": "DVM", "psyd": "PsyD", "edd": "EdD"}
SERIES_SUFFIX = re.compile(r"\s+(?:series|saga|trilogy|novels|books|collection|sequence|cycle|chronicles? series)$", re.I)
# Disc/part markers in folder names or titles: "Disc 2", "CD02", "Disk 3 of 9", "Part 1"
DISC = re.compile(r"\b(?:disc|disk|cd|part|pt)\.?\s*0*(\d{1,3})(?:\s*(?:of|/)\s*\d+)?\b", re.I)
# Volume-style sets: "Lessons 6-10", "Volume 2", "Book 3", "Level 2"
RANGE = re.compile(r"\b(lessons?|units?|volumes?|vol\.?|books?|episodes?|parts?)\s+(\d+)(?:\s*[-–]\s*(\d+))?", re.I)


def strip_unabridged(s):
    if not s:
        return s
    out = UNABRIDGED.sub("", s).strip()
    return out or s


def has_unabridged(s):
    return bool(s) and bool(UNABRIDGED.search(s)) and strip_unabridged(s) != s


def split_read_by(name):
    """'John Grisham read by Dennis Boutsikaris' -> ('John Grisham', 'Dennis Boutsikaris') or None."""
    m = READ_BY.match(name or "")
    if not m:
        return None
    author, narr = m.group("author").strip(" -–—,;("), m.group("narr").strip(" )")
    if not author or not narr:
        return None
    return author, narr


def is_credential(name):
    return (name or "").strip().lower().replace(" ", "") in CREDENTIALS


def credential_text(name):
    k = name.strip().lower().replace(".", "").replace(" ", "")
    return CRED_CASE.get(k, name.strip())


def fold(s):
    """Lowercase, no accents, no punctuation, single spaces."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def title_key(title):
    """Matching key for a book title: no '(Unabridged)', no subtitle, no leading article."""
    t = strip_unabridged(title or "")
    t = re.split(r"\s*[:–—]\s+|\s+-\s+", t, maxsplit=1)[0]
    t = fold(t)
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return t


def series_key(name):
    n = fold(SERIES_SUFFIX.sub("", (name or "").strip()))
    return re.sub(r"^(the)\s+", "", n)


def disc_number(*texts):
    for t in texts:
        m = DISC.search(t or "")
        if m:
            return int(m.group(1))
    return None


def natural_key(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s or "")]


def clean_seq(seq):
    """Keep plain numbers like 1, 2.5, 10. Box-set ranges like '1-3' return ''."""
    s = str(seq or "").strip()
    m = re.fullmatch(r"(?:book\s*)?(\d+(?:\.\d+)?)", s, re.I)
    if not m:
        return ""
    v = m.group(1)
    if "." in v:
        return v
    return v.lstrip("0") or "0"


def seq_sort(seq):
    try:
        return float(seq)
    except (TypeError, ValueError):
        return 1e9


_bad = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(s, fallback=""):
    """Usable as a folder name on any OS."""
    s = _bad.sub(" ", (s or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:120] or fallback
