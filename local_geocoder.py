# -*- coding: utf-8 -*-
"""
Local geocoding core based on SQLite + Levenshtein.
"""

import os
import sqlite3
import re

# DB_PATH is set via set_db_path() at plugin startup and persisted in
# QgsSettings. Fallback: profile directory (default path).
DB_PATH = os.path.join(os.path.dirname(__file__), 'geocoder.db')


def set_db_path(path):
    """Set the DB path at runtime (called at plugin startup from
    QgsSettings). Creates the directory if necessary."""
    global DB_PATH, _indexes_ensured, _lookup_checked
    DB_PATH = path
    # Path change: index/directory checks no longer apply to the new DB.
    _indexes_ensured = False
    _lookup_checked  = False
    _lookup_available.clear()
    dirpath = os.path.dirname(path)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)


_indexes_ensured  = False    # search indexes of this DB checked/created
_lookup_checked   = False    # Gemeinde/Ortsteil directory of this DB checked
_lookup_available = {}       # DB_PATH -> bool (directory tables present)


# ── Levenshtein ───────────────────────────────────────────────────────────────

def levenshtein(a, b):
    if a == b:
        return 0
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j]+1, curr[j-1]+1, prev[j-1]+(0 if ca==cb else 1)))
        prev = curr
    return prev[-1]


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize(text):
    """Lowercase, expand street abbreviations, remove dots."""
    if not text:
        return ''
    text = text.lower().strip()
    text = re.sub(r'\.(?=\s|$)', '', text)
    text = re.sub(r'(\w)str\b', r'\1straße', text)
    text = re.sub(r'\bstr\b', 'straße', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Additional canonicalization ONLY for comparing (Levenshtein) candidates that
# have already been fetched — not for the prefix query (that must match the
# normalize() form stored in the DB). Unifies hyphens, ß/ss and common
# abbreviations so that e.g. "St.-Anna-Str." and "Sankt Anna Straße" are
# recognized as equal.
_STREET_ABBREV = {'st': 'sankt', 'pl': 'platz', 'str': 'strasse'}

def _canonical(s):
    if not s:
        return ''
    s = s.replace('ß', 'ss')
    s = re.sub(r'[-/.]', ' ', s)               # hyphen/slash/dot → space
    s = re.sub(r'\s+', ' ', s).strip()
    return ' '.join(_STREET_ABBREV.get(w, w) for w in s.split())


# Canonical comparison form specifically for Gemeinde (municipality) names:
# unifies the official connective abbreviations that are written differently in
# HK/ALKIS data than in user/OSM input — 'a. d.'/'an der', 'v. d.'/'vor der',
# 'i. d.'/'in der', 'ob der', 'St.'/'Sankt' as well as ß/ss and separators. This
# makes e.g. "Rotenburg a. d. Fulda" and "Rotenburg an der Fulda" equal.
_GEM_CONNECTIVES = [
    (r'\b(?:an|a)\s+(?:der|dem|d)\b', 'ad'),
    (r'\b(?:vor|v)\s+(?:der|dem|d)\b', 'vd'),
    (r'\b(?:in|i)\s+(?:der|dem|d)\b', 'id'),
    (r'\bob\s+der\b', 'obd'),
]

def _canon_gem(s):
    if not s:
        return ''
    s = s.lower().replace('ß', 'ss')
    s = re.sub(r'[-/.]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'\bst\b', 'sankt', s)          # St. -> Sankt
    for pat, rep in _GEM_CONNECTIVES:
        s = re.sub(pat, rep, s)
    return re.sub(r'\s+', ' ', s).strip()


# Generic street name parts — unsuitable as an anchor for the second attempt.
_GENERIC_STREET_WORDS = {
    'strasse', 'platz', 'weg', 'gasse', 'allee', 'ring', 'damm', 'ufer',
    'steig', 'pfad', 'sankt', 'am', 'an', 'der', 'den', 'zum', 'zur',
    'auf', 'im', 'in', 'und', 'die', 'das',
}


# Official status suffixes that occur in Gemeinde names but are typically
# not entered by users. Examples from real HK data:
#   'Magdeburg, Landeshauptstadt' · 'Hanse- und Universitätsstadt Rostock'
#   'Greifswald, Universitäts- und Hansestadt' · 'Quedlinburg, Welterbestadt'
#   'Naumburg (Saale)' · 'Lutherstadt Wittenberg' / 'Wittenberg, Lutherstadt'
# _core_gemeinde() reduces such names to the core place name so that the
# search (exact match on gemeinde_norm) works reliably. Applied both at
# import (to gemeinde_norm) and at search time.
_GEMEINDE_STATUS_WORDS = [
    'landeshauptstadt', 'kreisstadt', 'große kreisstadt', 'kreisfreie stadt',
    'hansestadt', 'hanse- und universitätsstadt', 'universitäts- und hansestadt',
    'universitätsstadt', 'welterbestadt', 'lutherstadt', 'bergstadt',
    'dokumentationsstadt', 'wissenschaftsstadt', 'kur- und kreisstadt',
    'barlachstadt', 'eulenspiegelstadt', 'reuterstadt', 'inselstadt',
    'kurstadt', 'seestadt', 'seebad', 'ostseebad', 'ostseeheilbad', 'heilbad',
    'bad', 'stadt', 'hanse',
]


def _core_gemeinde(name):
    """Reduce a Gemeinde name to the core place name by removing official
    status additions (Landeshauptstadt, Hansestadt, Welterbestadt …) —
    whether they appear as prefix, suffix (with comma) or as a phrase.
    Parenthesized additions such as '(Saale)' are removed as well, since
    users usually enter only the main name.

    Examples:
      'magdeburg, landeshauptstadt'              -> 'magdeburg'
      'hanse- und universitätsstadt rostock'     -> 'rostock'
      'greifswald, universitäts- und hansestadt' -> 'greifswald'
      'naumburg (saale)'                         -> 'naumburg'
      'lutherstadt wittenberg'                   -> 'wittenberg'
    """
    n = normalize(name)
    if not n:
        return ''

    # Remove parenthesized additions: 'naumburg (saale)' -> 'naumburg'
    n = re.sub(r'\s*\([^)]*\)', '', n).strip()

    # Split off the comma part: 'magdeburg, landeshauptstadt' -> 'magdeburg'
    # The tail is discarded if it consists (as a contiguous phrase or as a
    # set of words) only of status designations.
    if ',' in n:
        head, _, tail = n.partition(',')
        head = head.strip()
        tail = tail.strip()
        tail_is_status = (
            tail in _GEMEINDE_STATUS_WORDS or
            all(w in _GEMEINDE_STATUS_WORDS or w in ('und', 'der', 'am')
                or w.rstrip('-') + 'stadt' in _GEMEINDE_STATUS_WORDS
                or w.endswith('-')
                for w in tail.split())
        )
        if head and tail_is_status:
            n = head

    # Remove status phrases at the start/end (longer ones first)
    changed = True
    while changed:
        changed = False
        for sw in sorted(_GEMEINDE_STATUS_WORDS, key=len, reverse=True):
            # Prefix: 'hanse- und universitätsstadt rostock' -> 'rostock'
            if n.startswith(sw + ' ') and len(n) > len(sw) + 1:
                rest = n[len(sw) + 1:].strip()
                if rest:
                    n = rest
                    changed = True
                    break
            # Suffix: 'rostock hansestadt' -> 'rostock'
            if n.endswith(' ' + sw) and len(n) > len(sw) + 1:
                rest = n[:-(len(sw) + 1)].strip()
                if rest:
                    n = rest
                    changed = True
                    break
    return n.strip()


def _gemeinde_match_core(g):
    """Core name of an already normalized Gemeinde name without the official
    addition: everything before the first '/' or '(' — e.g. 'schwedt/oder' -> 'schwedt',
    'oldenburg (oldb)' -> 'oldenburg', 'mühlhausen/thüringen' -> 'mühlhausen'."""
    return g.split('/')[0].split('(')[0].strip()


def _resolve_gemeinde(conn, gemeinde_n, bundesland=None):
    """Resolve an input Gemeinde to the gemeinde_norm value actually stored.
    If the exact name does not match (because the DB contains the official
    addition, e.g. 'Mühlhausen/Thüringen' or 'Schwedt/Oder'), an index-backed
    prefix query (idx_gem_str) looks for an entry whose CORE NAME matches the
    input exactly. This way genuinely different places (e.g. 'Neustadt-Glewe'
    for 'Neustrelitz') are NOT matched by mistake. Without a hit the input is
    returned unchanged."""
    if not gemeinde_n:
        return gemeinde_n
    # 1. Present exactly? (one index-backed LIMIT 1 check)
    sql    = "SELECT 1 FROM addresses WHERE gemeinde_norm = ?"
    params = [gemeinde_n]
    if bundesland:
        sql += " AND bundesland = ?"
        params.append(bundesland)
    sql += " LIMIT 1"
    if conn.execute(sql, params).fetchone():
        return gemeinde_n
    # 2. Canonical match: find the stored gemeinde_norm whose canonical form
    #    (abbreviations 'a. d.'/'an der', 'v. d.'/'vor der', 'St.'/'Sankt',
    #    ß/ss unified) matches the input — or whose core name (before '/' or
    #    '(') equals the input ("Mühlhausen" -> "Mühlhausen/Thüringen"). Only
    #    an index-backed prefix range is scanned, derived from the part of the
    #    name BEFORE the first connective abbreviation — fast, and does not hit
    #    unrelated places.
    target   = _canon_gem(gemeinde_n)
    tgt_core = _canon_gem(_gemeinde_match_core(gemeinde_n))
    sep  = re.sub(r'\s+', ' ', re.sub(r'[-/.]', ' ', gemeinde_n)).strip()
    toks = sep.split(' ')
    cut  = len(toks)
    for i, t in enumerate(toks):
        if i > 0 and t in ('an', 'a', 'vor', 'v', 'in', 'i', 'ob'):
            cut = i
            break
    base = ' '.join(toks[:cut]).strip() or sep
    prefixes = {base}
    if base.startswith('sankt '):
        prefixes.add('st ' + base[6:])
    elif base.startswith('st '):
        prefixes.add('sankt ' + base[3:])
    elif base == 'sankt':
        prefixes.add('st')

    best = None
    for pfx in prefixes:
        if not pfx:
            continue
        hi     = pfx[:-1] + chr(ord(pfx[-1]) + 1)
        sql    = ("SELECT DISTINCT gemeinde_norm FROM addresses "
                  "WHERE gemeinde_norm >= ? AND gemeinde_norm < ?")
        params = [pfx, hi]
        if bundesland:
            sql += " AND bundesland = ?"
            params.append(bundesland)
        sql += " LIMIT 1000"
        for row in conn.execute(sql, params).fetchall():
            gn = row[0]
            if not gn:
                continue
            gnc      = _canon_gem(gn)
            gnc_core = _canon_gem(_gemeinde_match_core(gn))
            if gnc == target or gnc_core == target or gnc == tgt_core:
                if best is None or len(gn) < len(best):
                    best = gn   # prefer the shortest matching name
    return best or gemeinde_n


# House number expression: number, optional letter, optional range/alternative
# ("12-14", "12 - 14", "12/14", "12+14", "12a-c", "12-14a").
_HN_EXPR = r'\d{1,5}\s*[a-zA-Z]?(?:\s*[-–/+]\s*\d*\s*[a-zA-Z]?)*'
_HN_EXPR_RE  = re.compile(r'^(' + _HN_EXPR + r')$')
_HN_TAIL_RE  = re.compile(r'\s+(' + _HN_EXPR + r')\s*$')
_HN_FIRST_RE = re.compile(r'^(\d+)\s*([a-zA-Z])?')


def normalize_hausnummer(h):
    """Bring a house number into comparison form: lowercase, no spaces,
    no 'Nr.' prefix; for ranges/alternatives the FIRST number counts
    ('12-14' → '12', '12 a' → '12a', '12/1' → '12', 'Nr. 7' → '7')."""
    h = (h or '').strip().lower()
    if not h:
        return ''
    h = re.sub(r'^(?:haus)?(?:nr|nummer|no)\.?\s*', '', h)
    if _HN_EXPR_RE.match(h):
        m = _HN_FIRST_RE.match(h)
        return m.group(1) + (m.group(2) or '')
    return re.sub(r'\s+', '', h)


def _extract_hausnummer(strasse_n):
    """Split the house number (incl. ranges like '12-14') off the end of the
    street text. Returns (street, normalized_house_number)."""
    m = _HN_TAIL_RE.search(strasse_n)
    if m:
        return strasse_n[:m.start()].strip(), normalize_hausnummer(m.group(1))
    return strasse_n, ''


# ── Bundesland detection (shared by worker and dialog) ───────────────────────

# PLZ prefix (first 2 digits) → Bundesland (matches the table in the help).
# Prefixes spanning several states are deliberately left unmapped — the
# search then runs without a Bundesland filter, Gemeinde/PLZ decide:
#   27 Bremen/Bremerhaven · 37 Göttingen (NI) ↔ Eschwege (HE) ↔ Beverungen (NRW)
#   49 Osnabrück (NI) ↔ NRW · 76 Karlsruhe (BW) ↔ Landau (RP)
# The Bundesland is only a pre-filter anyway: if the Gemeinde search fails
# with it, search() repeats it without the filter (see local_geocoder.search).
_PLZ_PREFIX_GROUPS = {
    'Nordrhein-Westfalen':    ['32', '33', '40', '41', '42', '44', '45', '46', '47',
                               '48', '49', '50', '51', '52', '53', '57', '58', '59'],
    'Hamburg':                ['20', '21', '22'],
    'Hessen':                 ['34', '35', '36', '60', '61', '63', '64', '65'],
    'Berlin':                 ['10', '12', '13', '14'],
    'Sachsen':                ['01', '02', '04', '08', '09'],
    'Brandenburg':            ['03', '15', '16'],
    'Sachsen-Anhalt':         ['06', '39'],
    'Thüringen':              ['07', '98', '99'],
    'Rheinland-Pfalz':        ['54', '55', '56', '67'],
    'Baden-Württemberg':      ['68', '69', '70', '71', '72', '73', '74', '75',
                               '77', '78', '79', '88', '89'],
    'Saarland':               ['66'],
    'Schleswig-Holstein':     ['23', '24', '25'],
    'Mecklenburg-Vorpommern': ['17', '18', '19'],
    'Bayern':                 ['80', '81', '82', '83', '84', '85', '86', '87',
                               '90', '91', '92', '93', '94', '95', '96', '97'],
    'Niedersachsen':          ['26', '29', '30', '31', '38'],
}
PLZ_PREFIX_TO_BUNDESLAND = {}
for _bl, _prefixes in _PLZ_PREFIX_GROUPS.items():
    for _p in _prefixes:
        assert _p not in PLZ_PREFIX_TO_BUNDESLAND, f'PLZ-Präfix {_p} doppelt'
        PLZ_PREFIX_TO_BUNDESLAND[_p] = _bl


def bundesland_from_plz(plz):
    """Bundesland from the PLZ prefix, or '' if unknown/ambiguous."""
    plz = (plz or '').strip()
    if len(plz) < 2:
        return ''
    return PLZ_PREFIX_TO_BUNDESLAND.get(plz[:2], '')


# Spellings of the Bundesland as they occur in user data (vehicle-plate/ISO
# abbreviations, without hyphen, with official prefix) → canonical name as in
# the DB. Keys are normalize() forms (lowercase, dots removed).
_BUNDESLAND_ALIASES = {
    'nordrhein-westfalen': 'Nordrhein-Westfalen', 'nordrhein westfalen': 'Nordrhein-Westfalen',
    'nrw': 'Nordrhein-Westfalen', 'nw': 'Nordrhein-Westfalen',
    'hamburg': 'Hamburg', 'hh': 'Hamburg', 'freie und hansestadt hamburg': 'Hamburg',
    'hessen': 'Hessen', 'he': 'Hessen', 'land hessen': 'Hessen',
    'berlin': 'Berlin', 'be': 'Berlin', 'land berlin': 'Berlin',
    'sachsen': 'Sachsen', 'sn': 'Sachsen', 'freistaat sachsen': 'Sachsen',
    'brandenburg': 'Brandenburg', 'bb': 'Brandenburg', 'land brandenburg': 'Brandenburg',
    'sachsen-anhalt': 'Sachsen-Anhalt', 'sachsen anhalt': 'Sachsen-Anhalt', 'st': 'Sachsen-Anhalt',
    'thüringen': 'Thüringen', 'thueringen': 'Thüringen', 'th': 'Thüringen',
    'freistaat thüringen': 'Thüringen',
    'rheinland-pfalz': 'Rheinland-Pfalz', 'rheinland pfalz': 'Rheinland-Pfalz',
    'rp': 'Rheinland-Pfalz', 'rlp': 'Rheinland-Pfalz',
    'baden-württemberg': 'Baden-Württemberg', 'baden württemberg': 'Baden-Württemberg',
    'baden-wuerttemberg': 'Baden-Württemberg', 'baden wuerttemberg': 'Baden-Württemberg',
    'bw': 'Baden-Württemberg',
    'saarland': 'Saarland', 'sl': 'Saarland',
    'schleswig-holstein': 'Schleswig-Holstein', 'schleswig holstein': 'Schleswig-Holstein',
    'sh': 'Schleswig-Holstein',
    'mecklenburg-vorpommern': 'Mecklenburg-Vorpommern',
    'mecklenburg vorpommern': 'Mecklenburg-Vorpommern', 'mv': 'Mecklenburg-Vorpommern',
    'bayern': 'Bayern', 'by': 'Bayern', 'freistaat bayern': 'Bayern',
    'niedersachsen': 'Niedersachsen', 'ni': 'Niedersachsen', 'nds': 'Niedersachsen',
    'bremen': 'Bremen', 'hb': 'Bremen', 'freie hansestadt bremen': 'Bremen',
}

# Generic country names that are NOT a Bundesland and are ignored.
GENERIC_COUNTRY_NAMES = {'deutschland', 'germany', 'de', 'deu', 'brd',
                         'bundesrepublik deutschland'}


def canonical_bundesland(text):
    """User spelling of a Bundesland → canonical name ('Nordrhein-
    Westfalen'), or '' if the text is not a (recognized) Bundesland.
    Accepts abbreviations (NRW, BW, BY …), missing hyphens and official
    prefixes ('Freistaat Bayern')."""
    n = normalize(text)
    if not n or n in GENERIC_COUNTRY_NAMES:
        return ''
    return _BUNDESLAND_ALIASES.get(n, '')


# ── Database access ───────────────────────────────────────────────────────────

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_indexes(conn):
    """Ensure the search indexes once. CREATE INDEX IF NOT EXISTS returns
    immediately when the indexes already exist (the normal case). If they are
    missing on a DB imported by an older version, they are built here once.

    The key one is the COMPOSITE index (gemeinde_norm, strasse_norm): only
    with it can a query 'gemeinde_norm = ? AND strasse_norm prefix' in a large
    city (e.g. Berlin) jump directly to the few matching rows instead of
    scanning the whole city."""
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        conn.executescript(
            "CREATE INDEX IF NOT EXISTS idx_bundesland   ON addresses(bundesland);"
            "CREATE INDEX IF NOT EXISTS idx_strasse_norm ON addresses(strasse_norm);"
            "CREATE INDEX IF NOT EXISTS idx_gem_str      ON addresses(gemeinde_norm, strasse_norm);"
            "CREATE INDEX IF NOT EXISTS idx_plz_str      ON addresses(plz, strasse_norm);"
        )
        conn.commit()
        _indexes_ensured = True
    except Exception:
        pass


def ensure_search_indexes(log=None):
    """Public entry point: creates missing search indexes (once).
    Returns True if they had to be built (for logging).

    With the optional log callback a message is emitted BEFORE the build
    (which may take several minutes on large databases) — otherwise the
    application appears frozen during the one-time index build."""
    global _indexes_ensured
    if _indexes_ensured:
        return False
    conn = get_connection()
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        need = {'idx_bundesland', 'idx_strasse_norm', 'idx_gem_str', 'idx_plz_str'}
        missing = need - have
        if missing and log:
            try:
                log('Die Datenbank wird einmalig für die Suche vorbereitet. '
                    'Das kann einige Minuten dauern …')
            except Exception:
                pass
        _ensure_indexes(conn)
        if missing and log:
            try:
                log('Datenbank vorbereitet.')
            except Exception:
                pass
        return bool(missing)
    finally:
        conn.close()




# ── Gemeinde/Ortsteil directory ──────────────────────────────────────────────
#
# Two small helper tables (~20,000 rows together) derived from the large
# address table:
#   gemeinde_index: per (gemeinde_norm, bundesland, gemeinde, epsg) the number
#                   of addresses, the centroid and a SHORT NAME without suffix
#                   ('frankfurt am main' -> 'frankfurt'). This makes it possible
#                   to recognize places with the same name and rank them by size.
#   ortsteil_index: alias names from the 'kreis' column (Hamburg Ortsteile,
#                   Berlin Bezirke, INSPIRE city districts, Kreis names) -> Gemeinde.
# They are built once on the first run (approx. 1 minute for 20 million
# addresses) and regenerated after every import. If they are missing (e.g.
# read-only DB), the search continues with the previous logic.

LOOKUP_SCHEMA_VERSION = 2   # 2: + bundesland_index (PLZ coverage)

# Connective words before which the short name of a place ends.
_KURZ_CONNECTIVES = {'am', 'an', 'a', 'im', 'in', 'i', 'bei', 'b', 'vor', 'v',
                     'ob', 'auf', 'ad', 'vd', 'id', 'obd'}


def _kurz_norm(gemeinde_norm):
    """Short name of a normalized Gemeinde name: the part before '/', '(' and
    before the first connective word — 'frankfurt am main' -> 'frankfurt',
    'bergen auf rügen' -> 'bergen', 'bad homburg vor der höhe' -> 'bad homburg'."""
    g = _gemeinde_match_core(gemeinde_norm or '')
    toks = g.split()
    cut = len(toks)
    for i, t in enumerate(toks):
        if i > 0 and t in _KURZ_CONNECTIVES:
            cut = i
            break
    return ' '.join(toks[:cut]).strip() or g


_KREIS_PREFIXES = ('Kreisfreie Stadt ', 'Landkreis ', 'Kreis ', 'Landeshauptstadt ',
                   'Stadt ', 'Bezirk ', 'Stadtbezirk ', 'Ortsteil ')


def _ortsteil_alias_norm(kreis):
    """Alias name from the kreis column: 'Rahlstedt,OT 0526 (Rahlstedt)' ->
    'rahlstedt', 'Kreisfreie Stadt Kassel' -> 'kassel'."""
    k = (kreis or '').split(',')[0]
    k = re.sub(r'\([^)]*\)', '', k).strip()
    for pre in _KREIS_PREFIXES:
        if k.startswith(pre):
            k = k[len(pre):]
            break
    return _core_gemeinde(k)


def rebuild_lookup_tables(conn, log=None):
    """Rebuild gemeinde_index / ortsteil_index / lookup_meta. Does NOT commit
    itself — the caller decides (import worker: inside the import
    transaction, followed by a single shared commit)."""
    if log:
        try:
            log('Suchverzeichnis wird aktualisiert (bei der bundesweiten '
                'Datenbank zwei bis drei Minuten) …')
        except Exception:
            pass
    for stmt in (
        "DROP TABLE IF EXISTS gemeinde_index",
        "DROP TABLE IF EXISTS ortsteil_index",
        "DROP TABLE IF EXISTS bundesland_index",
        "DROP TABLE IF EXISTS lookup_meta",
        "CREATE TABLE gemeinde_index (gemeinde_norm TEXT, kurz_norm TEXT, "
        "  bundesland TEXT, gemeinde TEXT, n INTEGER, cx REAL, cy REAL, epsg INTEGER)",
        "CREATE TABLE ortsteil_index (alias_norm TEXT, gemeinde_norm TEXT, "
        "  bundesland TEXT, n INTEGER)",
        "CREATE TABLE bundesland_index (bundesland TEXT PRIMARY KEY, n INTEGER, "
        "  n_ohne_plz INTEGER)",
        "CREATE TABLE lookup_meta (key TEXT PRIMARY KEY, value TEXT)",
    ):
        conn.execute(stmt)

    # One scan yields the Gemeinde directory AND the PLZ coverage per Bundesland
    # (rows without a Gemeinde count towards coverage too). Deliberately NO
    # coalesce() in the GROUP BY: it prevents index access via idx_gem_str
    # and took about 270 instead of 165 seconds on 21 million addresses.
    rows = conn.execute(
        "SELECT gemeinde_norm, bundesland, gemeinde, epsg, COUNT(*), "
        "       AVG(x), AVG(y), SUM(CASE WHEN plz IS NULL OR plz = '' THEN 1 ELSE 0 END) "
        "FROM addresses GROUP BY 1, 2, 3, 4").fetchall()
    conn.executemany(
        "INSERT INTO gemeinde_index VALUES (?,?,?,?,?,?,?,?)",
        [(gn, _kurz_norm(gn), bl, g, n, cx, cy, e)
         for gn, bl, g, e, n, cx, cy, _o in rows if gn])
    bl_agg = {}
    for _gn, bl, _g, _e, n, _cx, _cy, ohne in rows:
        a = bl_agg.setdefault(bl, [0, 0])
        a[0] += n
        a[1] += ohne or 0
    conn.executemany("INSERT INTO bundesland_index VALUES (?,?,?)",
                     [(bl, n, o) for bl, (n, o) in bl_agg.items()])

    rows = conn.execute(
        "SELECT kreis, gemeinde_norm, bundesland, COUNT(*) FROM addresses "
        "WHERE kreis IS NOT NULL AND kreis <> '' AND gemeinde_norm <> '' "
        "GROUP BY 1, 2, 3").fetchall()
    agg = {}
    for kreis, gn, bl, n in rows:
        a = _ortsteil_alias_norm(kreis)
        if not a or a == gn:
            continue
        key = (a, gn, bl)
        agg[key] = agg.get(key, 0) + n
    conn.executemany("INSERT INTO ortsteil_index VALUES (?,?,?,?)",
                     [(a, gn, bl, n) for (a, gn, bl), n in agg.items()])

    for stmt in (
        "CREATE INDEX idx_gi_norm  ON gemeinde_index(gemeinde_norm)",
        "CREATE INDEX idx_gi_kurz  ON gemeinde_index(kurz_norm)",
        "CREATE INDEX idx_oi_alias ON ortsteil_index(alias_norm)",
    ):
        conn.execute(stmt)
    total = conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    conn.execute("INSERT INTO lookup_meta VALUES ('address_count', ?)", (str(total),))
    conn.execute("INSERT INTO lookup_meta VALUES ('schema_version', ?)",
                 (str(LOOKUP_SCHEMA_VERSION),))
    _lookup_available[DB_PATH] = True


def lookup_tables_ready(conn):
    """True if the directory exists, the schema is current and the address
    count matches the state at build time (otherwise stale, e.g. after an
    import with an older plugin version)."""
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {'gemeinde_index', 'ortsteil_index', 'lookup_meta'} <= have:
            return False
        meta = dict((r[0], r[1]) for r in
                    conn.execute("SELECT key, value FROM lookup_meta").fetchall())
        if meta.get('schema_version') != str(LOOKUP_SCHEMA_VERSION):
            return False
        total = conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
        return meta.get('address_count') == str(total)
    except Exception:
        return False


def ensure_lookup_tables(log=None):
    """Check the directory once and (re)build it if needed. Returns True if
    it was built. If the build fails (e.g. read-only DB), the search
    continues with the previous logic."""
    global _lookup_checked
    if _lookup_checked or not db_exists():
        return False
    conn = get_connection()
    try:
        if lookup_tables_ready(conn):
            _lookup_checked = True
            _lookup_available[DB_PATH] = True
            return False
        rebuild_lookup_tables(conn, log)
        conn.commit()
        _lookup_checked = True
        if log:
            try:
                log('Suchverzeichnis aktualisiert.')
            except Exception:
                pass
        return True
    except Exception:
        _lookup_available[DB_PATH] = False
        return False
    finally:
        conn.close()


def _has_lookup(conn):
    """Fast, cached check whether the directory tables exist."""
    v = _lookup_available.get(DB_PATH)
    if v is None:
        try:
            v = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='gemeinde_index'").fetchone() is not None
        except Exception:
            v = False
        _lookup_available[DB_PATH] = v
    return v


# Ortsteil/Kreis aliases pointing to more Gemeinden than this are Landkreise —
# too vague as a place specification, they are ignored.
_MAX_ALIAS_GEMEINDEN = 3
# 'City-Ortsteil' spelling ('Berlin-Mitte', 'Hamburg-Altona'): the part before
# the hyphen only counts if it is a LARGE city — otherwise 'Bergen-Enkheim'
# (a Frankfurt district) would end up in Bergen.
_MIN_HYPHEN_CITY_SIZE = 20000


def _display_key(name):
    """Comparison form of the DISPLAY spelling of a place incl. suffix:
    'Frankfurt (Oder)', 'Frankfurt/Oder', 'frankfurt oder' -> 'frankfurt oder'."""
    n = normalize(name)
    n = re.sub(r'[()\-/.,]', ' ', n)
    return re.sub(r'\s+', ' ', n).strip()


def _plz_orte(conn, plz_n):
    """(gemeinde_norm, bundesland) pairs occurring under this PLZ — very
    fast via idx_plz_str; picks the right one among places with the same
    name (Halle + 33790 -> Halle (Westf.))."""
    if not plz_n:
        return set()
    try:
        return {(r[0] or '', r[1]) for r in conn.execute(
            "SELECT gemeinde_norm, bundesland FROM addresses WHERE plz = ? LIMIT 400",
            (plz_n,)).fetchall()}
    except Exception:
        return set()


def _gemeinde_targets(conn, gemeinde_n, bundesland=None, _depth=0,
                      raw=None, plz=None):
    """Target places for a place input, best first: list of
    (gemeinde_norm, bundesland, n). Order:
      1. places occurring under the given PLZ,
      2. matching Bundesland,
      3. exact display spelling incl. suffix ('Frankfurt (Oder)'),
      4. size (address count) — 'Frankfurt' more likely means Frankfurt am Main.

    Resolves: places with the same name ('Halle' -> Halle (Saale) before
    Halle (Westf.)), short names ('Frankfurt' -> 'Frankfurt am Main' AND
    'Frankfurt (Oder)'), official connective abbreviations (via
    _resolve_gemeinde), Ortsteile/Bezirke ('Blankenese' -> Hamburg,
    'Pankow' -> Berlin) as well as 'City-Ortsteil' ('Berlin-Mitte' -> Berlin).
    Returns None if no directory exists (the caller then uses the previous
    logic), [] if the place is unknown."""
    if not gemeinde_n:
        return []
    if not _has_lookup(conn):
        return None
    q = ("SELECT gemeinde_norm, bundesland, SUM(n) AS n, "
         "       GROUP_CONCAT(DISTINCT gemeinde) AS namen FROM gemeinde_index "
         "WHERE gemeinde_norm = ? OR kurz_norm = ? GROUP BY 1, 2")
    used = gemeinde_n   # name under which the match was actually found
    rows = [tuple(r) for r in conn.execute(q, (gemeinde_n, gemeinde_n)).fetchall()]
    if not rows:
        resolved = _resolve_gemeinde(conn, gemeinde_n, bundesland)
        if resolved != gemeinde_n:
            rows = [tuple(r) for r in conn.execute(q, (resolved, resolved)).fetchall()]
    if not rows:
        # 'halle/saale' -> core name 'halle'; the display spelling
        # ('Halle (Saale)') then decides the order below.
        core = _gemeinde_match_core(gemeinde_n)
        if core and core != gemeinde_n:
            rows = [tuple(r) for r in conn.execute(q, (core, core)).fetchall()]
            raw  = raw or gemeinde_n
            used = core
    if not rows:
        rows = [tuple(r) + ('',) for r in conn.execute(
            "SELECT gemeinde_norm, bundesland, SUM(n) AS n FROM ortsteil_index "
            "WHERE alias_norm = ? GROUP BY 1, 2", (gemeinde_n,)).fetchall()]
        if len(rows) > _MAX_ALIAS_GEMEINDEN:
            rows = []
    if not rows and _depth == 0 and '-' in gemeinde_n:
        head = gemeinde_n.split('-')[0].strip()
        if head:
            sub = _gemeinde_targets(conn, head, bundesland, _depth=1, plz=plz) or []
            rows = [tuple(r) + ('',) for r in sub if r[2] >= _MIN_HYPHEN_CITY_SIZE]
    if not rows:
        return []

    # An exact display spelling ('Frankfurt (Oder)' entered) beats the short-name
    # heuristic — but only if the user actually wrote a suffix.
    raw_key = _display_key(raw) if raw else ''
    def _display_hit(r):
        if not raw_key or raw_key == _display_key(used):
            return 1
        namen = (r[3] or '').split(',')
        return 0 if any(_display_key(nm) == raw_key for nm in namen) else 1

    plz_orte = _plz_orte(conn, plz) if plz else set()
    rows.sort(key=lambda r: (0 if (r[0], r[1]) in plz_orte else 1,
                             0 if (bundesland and r[1] == bundesland) else 1,
                             _display_hit(r),
                             -(r[2] or 0)))
    return [(r[0], r[1], r[2]) for r in rows[:8]]


def db_exists():
    if not os.path.exists(DB_PATH):
        return False
    try:
        conn = get_connection()
        # LIMIT 1 instead of COUNT(*) — on large DBs (millions of rows) a full
        # COUNT scan is expensive and unnecessary; we only want to know whether
        # any row exists at all.
        row = conn.execute("SELECT 1 FROM addresses LIMIT 1").fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _table_has_index(conn, index_name):
    try:
        r = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)).fetchone()
        return r is not None
    except Exception:
        return False


def get_db_stats(db_path=None):
    """Number of addresses per Bundesland. On very large databases this
    GROUP BY query can take several seconds — callers should therefore not
    run it in the UI thread (see DbStatsWorker in the dialog).
    db_path: optional alternative path (default: active DB_PATH)."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return {}
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # An index on bundesland speeds up GROUP BY considerably. Create it
        # once if not yet present (idempotent, IF NOT EXISTS).
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bundesland "
                "ON addresses(bundesland)")
            conn.commit()
        except Exception:
            pass
        rows = conn.execute(
            "SELECT bundesland, COUNT(*) AS n FROM addresses "
            "GROUP BY bundesland ORDER BY bundesland"
        ).fetchall()
        conn.close()
        return {r['bundesland']: r['n'] for r in rows}
    except Exception:
        return {}


# ── Source metadata (automatic download) ─────────────────────────────────────

SOURCE_META_FIELDS = ('quelle', 'url', 'etag', 'last_modified', 'sha256', 'dateien',
                      'heruntergeladen_am', 'importiert_am', 'adressen')


def ensure_source_meta(conn):
    """Table holding the state of the automatically downloaded source per Bundesland."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_meta ("
        " bundesland TEXT PRIMARY KEY, quelle TEXT, url TEXT, etag TEXT,"
        " last_modified TEXT, sha256 TEXT, dateien TEXT,"
        " heruntergeladen_am TEXT, importiert_am TEXT, adressen INTEGER)")


def _read_rows(sql, params=(), db_path=None, table=None):
    """Small read helper for the UI: short timeout (a running import may hold
    the write lock — better an empty result than a hanging UI), and NO
    table creation (read-only)."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(path, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            if table and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone() is None:
                return []
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def get_source_meta(bundesland, db_path=None):
    """Latest source state of a Bundesland, or None."""
    rows = _read_rows("SELECT * FROM source_meta WHERE bundesland = ?", (bundesland,),
                      db_path, table='source_meta')
    return rows[0] if rows else None


def all_source_meta(db_path=None):
    return {r['bundesland']: r for r in
            _read_rows("SELECT * FROM source_meta", (), db_path, table='source_meta')}


# ── PLZ coverage and PLZ completion ──────────────────────────────────────────

def get_plz_coverage(db_path=None):
    """{bundesland: (addresses, of_which_without_plz)} from the directory
    (bundesland_index, available immediately) — or None if the directory
    still uses the old schema or is missing."""
    rows = _read_rows("SELECT bundesland, n, n_ohne_plz FROM bundesland_index", (),
                      db_path, table='bundesland_index')
    if not rows:
        return None
    return {r['bundesland']: (r['n'] or 0, r['n_ohne_plz'] or 0) for r in rows}


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def ensure_plz_herkunft(conn):
    """Create the column addresses.plz_herkunft if missing ('gebiet' = PLZ
    filled in from a PLZ area, empty = PLZ from the source). ALTER TABLE ADD
    COLUMN only changes the schema — instant even with 20 million addresses."""
    if 'plz_herkunft' not in _columns(conn, 'addresses'):
        conn.execute("ALTER TABLE addresses ADD COLUMN plz_herkunft TEXT")


def ensure_plz_meta(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plz_meta ("
        " bundesland TEXT PRIMARY KEY, layer TEXT, feld TEXT,"
        " ergaenzt INTEGER, ohne_flaeche INTEGER, am TEXT, quelle_key TEXT)")
    if 'quelle_key' not in _columns(conn, 'plz_meta'):
        conn.execute("ALTER TABLE plz_meta ADD COLUMN quelle_key TEXT")


def refresh_plz_coverage(conn, bundesland):
    """Update the PLZ coverage of a Bundesland in the directory
    (bundesland_index) after a PLZ completion — without rebuilding the whole
    directory. Uses the index on plz (empty PLZ are few)."""
    if not _table_exists(conn, 'bundesland_index'):
        return
    ohne = conn.execute("SELECT COUNT(*) FROM addresses WHERE plz = '' AND bundesland = ?",
                        (bundesland,)).fetchone()[0]
    ohne += conn.execute("SELECT COUNT(*) FROM addresses WHERE plz IS NULL AND bundesland = ?",
                         (bundesland,)).fetchone()[0]
    conn.execute("UPDATE bundesland_index SET n_ohne_plz = ? WHERE bundesland = ?",
                 (ohne, bundesland))


def record_plz_meta(conn, bundesland, info, replace):
    """Record the PLZ completion of an import (inside its transaction,
    without commit). info = dict(layer, feld, ergaenzt, ohne_flaeche) or None
    (import without PLZ layer). A replacing import without a layer deletes the
    old entry — the completion is then no longer part of the data.
    An appending import (replace=False) adds up the counters."""
    ensure_plz_meta(conn)
    if info is None:
        if replace:
            conn.execute("DELETE FROM plz_meta WHERE bundesland = ?", (bundesland,))
        return
    from datetime import datetime
    am  = datetime.now().isoformat(timespec='seconds')
    old = conn.execute("SELECT ergaenzt, ohne_flaeche FROM plz_meta WHERE bundesland = ?",
                       (bundesland,)).fetchone()
    erg, ohne = int(info.get('ergaenzt') or 0), int(info.get('ohne_flaeche') or 0)
    if old and not replace:
        erg  += old[0] or 0
        ohne += old[1] or 0
    conn.execute("INSERT OR REPLACE INTO plz_meta "
                 "(bundesland, layer, feld, ergaenzt, ohne_flaeche, am, quelle_key) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (bundesland, info.get('layer', ''), info.get('feld', ''), erg, ohne, am,
                  info.get('quelle_key')))


def resolver_info(resolver, source, field, layername=None):
    """Helper for the import workers: PLZ meta info from a resolver."""
    if resolver is None:
        return None
    if hasattr(resolver, 'info'):           # ResolverSession (plz_resolver)
        return resolver.info()
    st = resolver.stats()
    name = os.path.basename(source or '')
    if layername:
        name += f' ({layername})'
    return {'layer': name, 'feld': field or '', 'ergaenzt': st.get('matched', 0),
            'ohne_flaeche': st.get('missed', 0)}


def all_plz_meta(db_path=None):
    return {r['bundesland']: r for r in
            _read_rows("SELECT * FROM plz_meta", (), db_path, table='plz_meta')}


def save_source_meta(bundesland, db_path=None, **fields):
    """Save/update the state of a source (known fields only)."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        ensure_source_meta(conn)
        cols = [k for k in SOURCE_META_FIELDS if k in fields]
        conn.execute(
            "INSERT INTO source_meta (bundesland" + ''.join(f', {c}' for c in cols) + ") "
            "VALUES (?" + ', ?' * len(cols) + ") "
            "ON CONFLICT(bundesland) DO UPDATE SET "
            + ', '.join(f'{c} = excluded.{c}' for c in cols),
            [bundesland] + [fields[c] for c in cols])
        conn.commit()
    finally:
        conn.close()


def diagnose_search(strasse, plz='', gemeinde='', bundesland=None):
    """Explain why a local search returns no hit. Returns a short
    diagnostic string (for the geocoding log). Checks step by step:
    DB present → Bundesland in DB → Gemeinde in DB (in the Bundesland)
    → street in the Gemeinde."""
    if not db_exists():
        return 'Keine lokale Datenbank vorhanden'

    conn = get_connection()
    _ensure_indexes(conn)
    try:
        gemeinde_n = _core_gemeinde(gemeinde) if gemeinde else ''
        plz_n      = (plz or '').strip()
        strasse_n, _ = _extract_hausnummer(normalize(strasse))

        # Bundesland in the DB?
        if bundesland:
            hit = conn.execute(
                "SELECT 1 FROM addresses WHERE bundesland = ? LIMIT 1",
                (bundesland,)).fetchone()
            if hit is None:
                return f'Bundesland {bundesland} ist in der lokalen Datenbank nicht enthalten'

        # Gemeinde in the DB at all? (WITHOUT Bundesland filter — it may only be
        # guessed from the PLZ prefix and thus be wrong, e.g. 21xxx -> Hamburg,
        # although the Gemeinde lies in Niedersachsen.)
        if gemeinde_n:
            targets = _gemeinde_targets(conn, gemeinde_n, None,
                                        raw=gemeinde, plz=plz_n)
            if targets:
                resolved = targets[0][0]
            else:
                resolved = _resolve_gemeinde(conn, gemeinde_n, None)
            hit = conn.execute(
                "SELECT 1 FROM addresses WHERE gemeinde_norm = ? LIMIT 1",
                (resolved,)).fetchone()
            if hit is None:
                # Does the Gemeinde exist under a similar name? A range query
                # on the prefix uses the gemeinde_norm index (no '%…%' scan).
                prefix = _safe_prefix(gemeinde_n)
                sample = []
                if prefix:
                    sample = conn.execute(
                        "SELECT DISTINCT gemeinde FROM addresses "
                        "WHERE gemeinde_norm >= ? AND gemeinde_norm < ? LIMIT 3",
                        [prefix, prefix + '\uffff']).fetchall()
                hint = ''
                if sample:
                    namen = ', '.join(r['gemeinde'] for r in sample)
                    hint = f' – ähnlich: {namen}'
                return f'Ort „{gemeinde}“ ist in der lokalen Datenbank nicht enthalten{hint}'
            gemeinde_n = resolved   # use the resolved DB name for the street check

        # Street in the Gemeinde (or, if no Gemeinde, in the PLZ)?
        # ss/ß-robust prefix so that "Grosse" also finds "Große…".
        pfx = _safe_prefix(strasse_n, 8)
        if strasse_n and pfx and (gemeinde_n or plz_n):
            if gemeinde_n:
                hit = conn.execute(
                    "SELECT 1 FROM addresses WHERE gemeinde_norm = ? "
                    "AND strasse_norm LIKE ? LIMIT 1",
                    (gemeinde_n, pfx + '%')).fetchone()
                if hit is None:
                    return f'Straße „{strasse}“ in {gemeinde} nicht gefunden'
            else:
                hit = conn.execute(
                    "SELECT 1 FROM addresses WHERE plz = ? "
                    "AND strasse_norm LIKE ? LIMIT 1",
                    (plz_n, pfx + '%')).fetchone()
                if hit is None:
                    return f'Straße „{strasse}“ in PLZ {plz_n} nicht gefunden'

        return 'Keine ausreichend ähnliche Adresse gefunden'
    except Exception:
        return 'Ursache konnte nicht ermittelt werden'
    finally:
        conn.close()


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS addresses (
            id            INTEGER PRIMARY KEY,
            bundesland    TEXT NOT NULL,
            kreis         TEXT,
            gemeinde      TEXT,
            plz           TEXT,
            strasse       TEXT NOT NULL,
            hausnummer    TEXT,
            hausnr_zusatz TEXT,
            epsg          INTEGER,
            x             REAL NOT NULL,
            y             REAL NOT NULL,
            strasse_norm  TEXT,
            gemeinde_norm TEXT,
            plz_herkunft  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bundesland   ON addresses(bundesland);
        CREATE INDEX IF NOT EXISTS idx_strasse_norm ON addresses(strasse_norm);
        CREATE INDEX IF NOT EXISTS idx_gem_str      ON addresses(gemeinde_norm, strasse_norm);
        CREATE INDEX IF NOT EXISTS idx_plz_str      ON addresses(plz, strasse_norm);
    """)
    ensure_plz_herkunft(conn)     # older databases
    conn.commit()


# ── Search ────────────────────────────────────────────────────────────────────

# Minimum similarity for the Gemeinde — below it the candidate is rejected outright.
# Prevents "Berlin" from yielding a hit in Hamburg.
_GEMEINDE_MIN_SCORE = 0.75


def _safe_prefix(s, maxlen=5):
    """Return a street name prefix for the index-backed range query that is
    robust against different ß/ss spellings.

    The DB stores strasse_norm in the official spelling (e.g. "große
    straße"), but the input often uses "ss" ("grosse straße"). Since "ß"
    (U+00DF) sorts after "ss", a prefix "gross" would exclude the DB entry
    "große…" from the byte range [gross, gross+\\uffff). Therefore the prefix
    is cut before the first ß/ss position — the Levenshtein ranking (which
    unifies ß/ss anyway) then decides among the few remaining candidates.
    Umlauts are left untouched (they are usually entered correctly) so as not
    to reduce selectivity unnecessarily."""
    if not s:
        return ''
    p = s[:maxlen]
    cut = len(p)
    i = p.find('ß')
    if i != -1:
        cut = min(cut, i)
    j = p.find('ss')
    if j != -1:
        cut = min(cut, j)
    return p[:cut]


def search(strasse, hausnummer='', plz='', gemeinde='', bundesland=None,
           max_results=5, fuzzy_threshold=0.30):
    """
    Search with strict Gemeinde check:
    1. Index-backed range query (gemeinde_norm + strasse_norm prefix)
    2. Levenshtein ranking; Gemeinde below _GEMEINDE_MIN_SCORE → candidate rejected
    """
    if not db_exists():
        return []

    conn = get_connection()
    _ensure_indexes(conn)

    if hausnummer:
        # House number comes as a separate field -> do NOT trim the street name.
        # Otherwise numbered street names ("Straße 10", "T 6", "An der B 5")
        # would be wrongly shortened (the number belongs to the name here, not
        # to the house number).
        strasse_n = normalize(strasse)
        hausnummer = normalize_hausnummer(hausnummer)
        # If the same number also appears at the end of the street text
        # ("Elchweg 3" + house number "3"), remove it there.
        s2, hn2 = _extract_hausnummer(strasse_n)
        if hn2 and hn2 == hausnummer:
            strasse_n = s2
    else:
        # Combined input ("Lindenstraße 12", "Lindenstraße 12-14") ->
        # split off the trailing house number (for ranges the first number counts).
        strasse_n, hausnummer = _extract_hausnummer(normalize(strasse))
    hausnr_n   = hausnummer or ''
    gemeinde_n = _core_gemeinde(gemeinde) if gemeinde else ''
    plz_n      = (plz or '').strip()
    strasse_c  = _canonical(strasse_n)   # canonical comparison form of the input

    # ── Fetch candidates (index-backed, without FTS) ─────────────────────────
    # The locality anchor is the Gemeinde (gemeinde_norm) OR the PLZ. A 5-digit
    # PLZ is unique nationwide and thus more precise than a Bundesland merely
    # guessed from the PLZ prefix — so the PLZ search sets NO Bundesland
    # filter. A pure range query over (gemeinde_norm|plz, strasse_norm) uses
    # idx_gem_str or idx_plz_str and returns within a few milliseconds even in
    # large cities.
    _fc_state = {'best': 0.0}   # best street similarity of the last call

    def _fetch_candidates(gem=None, use_plz=False, use_bundesland=True, bl=None):
        # Build the locality WHERE clause. bl: explicit Bundesland (from the
        # Gemeinde directory) instead of the guessed parameter.
        _fc_state['best'] = 0.0
        loc_sql    = ""
        loc_params = []
        if gem:
            loc_sql += " AND gemeinde_norm = ?"
            loc_params.append(gem)
        if use_plz and plz_n:
            loc_sql += " AND plz = ?"
            loc_params.append(plz_n)
        if bl:
            loc_sql += " AND bundesland = ?"
            loc_params.append(bl)
        elif use_bundesland and bundesland:
            loc_sql += " AND bundesland = ?"
            loc_params.append(bundesland)

        prefix = _safe_prefix(strasse_n)
        rng_sql, rng_params = "", []
        if prefix:
            # Range query instead of LIKE: uses idx_gem_str / idx_plz_str.
            # _safe_prefix stops at ß/ss (Grosse/Große, Strasse/Straße).
            rng_sql    = " AND strasse_norm >= ? AND strasse_norm < ?"
            rng_params = [prefix, prefix + '\uffff']

        # Stage 1: fetch distinct street names in scope and rank them by
        # similarity. Prevents a common, alphabetically earlier street with
        # many house numbers from pushing the exactly matching street out of
        # the row limit (e.g. "Karl-Marx-Allee" otherwise returned
        # "Karl-Lade-Straße"). Distinct street names are few even in large cities.
        street_rows = conn.execute(
            "SELECT DISTINCT strasse_norm FROM addresses WHERE 1=1"
            + loc_sql + rng_sql + " LIMIT 5000",
            loc_params + rng_params).fetchall()
        if not street_rows:
            return []
        ranked = []
        for r in street_rows:
            sn = r['strasse_norm'] or ''
            sc = _canonical(sn)
            s_sc = 1.0 - levenshtein(strasse_c, sc) / max(len(strasse_c), len(sc), 1)
            if s_sc >= fuzzy_threshold:
                ranked.append((s_sc, sn))
        if not ranked:
            return []
        ranked.sort(key=lambda t: t[0], reverse=True)
        _fc_state['best'] = ranked[0][0]
        top_streets = [sn for _, sn in ranked[:8]]

        # Stage 2: fetch only the rows of the best streets. The ordering puts the
        # best street (and, if given, the exactly matching house number) first
        # so it is not cut off by the limit.
        ph = ",".join("?" * len(top_streets))
        order_sql    = " ORDER BY CASE WHEN strasse_norm = ? THEN 0 ELSE 1 END"
        order_params = [top_streets[0]]
        if hausnr_n:
            order_sql += (", CASE WHEN replace(lower(hausnummer)"
                          " || coalesce(lower(hausnr_zusatz),''),' ','') = ?"
                          " THEN 0 ELSE 1 END")
            order_params.append(hausnr_n)
        return conn.execute(
            "SELECT * FROM addresses WHERE 1=1" + loc_sql
            + f" AND strasse_norm IN ({ph})" + order_sql + " LIMIT 1500",
            loc_params + top_streets + order_params).fetchall()

    candidates = []
    hinweis    = ''
    if strasse_n:
        # 1) Gemeinde known → Gemeinde-based. The Gemeinde directory returns
        #    the target places in a sensible order (matching Bundesland,
        #    exact name, then size) and resolves places with the same name,
        #    short names ('Frankfurt' → Frankfurt am Main / Frankfurt (Oder)),
        #    Ortsteile ('Blankenese' → Hamburg) and 'Berlin-Mitte'.
        if gemeinde_n:
            targets = _gemeinde_targets(conn, gemeinde_n, bundesland,
                                        raw=gemeinde, plz=plz_n)
            # Did the user write a distinguishing suffix ('Frankfurt (Oder)')?
            # Then the place is not ambiguous.
            zusatz = (_display_key(gemeinde)
                      != _display_key(_gemeinde_match_core(gemeinde_n)))
            if targets is None:
                # No directory (read-only/old DB): previous logic.
                candidates = _fetch_candidates(gem=gemeinde_n)
                if not candidates:
                    resolved = _resolve_gemeinde(conn, gemeinde_n, bundesland)
                    if resolved != gemeinde_n:
                        gemeinde_n = resolved
                        candidates = _fetch_candidates(gem=gemeinde_n)
                if not candidates and bundesland:
                    candidates = _fetch_candidates(gem=gemeinde_n, use_bundesland=False)
            else:
                # Check target places in order; the place with the BEST street
                # hit wins (not the first one with any fuzzy candidate):
                # „Karl-Marx-Straße, Frankfurt" lies in Frankfurt (Oder), not
                # as „Karl-Albert-Straße" in Frankfurt am Main.
                # A (nearly) exact hit ends the check immediately.
                # If there are target places matching the PLZ or the
                # Bundesland, the choice stays restricted to those (a place
                # „Halle, NRW" must not end up in Halle (Saale) just because
                # the street exists there).
                plz_orte = _plz_orte(conn, plz_n)
                passend  = [t for t in targets
                            if (t[0], t[1]) in plz_orte or (bundesland and t[1] == bundesland)]
                auswahl  = passend or targets
                best = None   # (street_score, candidates, gemeinde_norm, bundesland)
                for gn, bl, _n in auswahl:
                    cand = _fetch_candidates(gem=gn, bl=bl)
                    if not cand:
                        continue
                    sc = _fc_state['best']
                    if best is None or sc > best[0] + 1e-9:
                        best = (sc, cand, gn, bl)
                    if sc >= 0.95:
                        break
                if best is not None:
                    candidates, gemeinde_n = best[1], best[2]
                    # Several places with the same name and no disambiguation
                    # (no PLZ, no Bundesland, no suffix) → note it.
                    orte = {(t[0], t[1]) for t in targets}
                    if len(orte) > 1 and not plz_n and not bundesland and not zusatz:
                        hinweis = (f'Ort mehrdeutig ({len(orte)} Orte) — '
                                   f'{best[2]} ({best[3]}) gewählt')
                elif targets:
                    gemeinde_n = targets[0][0]
        # 2) PLZ anchor: if no Gemeinde is known OR the Gemeinde search came
        #    up empty (place missing, misspelled, or guessed Bundesland
        #    wrong). PLZ is unique nationwide → without Bundesland.
        if not candidates and plz_n:
            candidates = _fetch_candidates(use_plz=True, use_bundesland=False)
        # 3) Last attempt without a locality anchor (street only) — deliberately
        #    only when neither Gemeinde nor PLZ is given.
        if not candidates and not gemeinde_n and not plz_n:
            candidates = _fetch_candidates()

    # ── Second attempt: only if the prefix search came up EMPTY and a Gemeinde
    # is known (then limited to that town, no nationwide scan). Anchors on the
    # longest distinctive word of the street name — catches cases where the
    # beginning differs (e.g. "Karl-Renner-Str." vs. official "Dr.-Karl-Renner-Str.",
    # "Sankt-Anna-Str." vs. "St.-Anna-Str."). Runs rarely and only over the
    # few rows of one town.
    if not candidates and gemeinde_n and strasse_n:
        woerter = [w for w in re.split(r'[-\s]+', strasse_n)
                   if len(w) >= 4 and _canonical(w) not in _GENERIC_STREET_WORDS]
        if woerter:
            anker = max(woerter, key=len)   # in DB spelling (with ß), matches strasse_norm
            sql    = "SELECT * FROM addresses WHERE gemeinde_norm = ?"
            params = [gemeinde_n]
            if bundesland:
                sql += " AND bundesland = ?"
                params.append(bundesland)
            sql += " AND strasse_norm LIKE ? LIMIT 400"
            params.append(f'%{anker}%')
            candidates = conn.execute(sql, params).fetchall()

    conn.close()

    if not candidates:
        return []

    # ── Step 3: Levenshtein ranking ───────────────────────────────────────────
    scored = []
    for row in candidates:
        row_strasse_n  = row['strasse_norm'] or ''
        row_gemeinde_n = row['gemeinde_norm'] or ''

        # Determine the PLZ match up front — an exact PLZ is unique nationwide
        # and already secures the locality, even if the place name differs.
        # 'plz_usable' distinguishes: the DB row HAS a PLZ (then the user's PLZ
        # can be confirmed) vs. a DB row without PLZ (common for ALKIS without
        # PLZ intersection) — then the PLZ is treated neutrally, not penalized.
        row_plz    = (row['plz'] or '').strip()
        plz_usable = bool(plz_n and row_plz)
        plz_match  = bool(plz_usable and plz_n == row_plz)

        # Gemeinde: hard minimum — reject only if the Gemeinde matches too
        # weakly AND the PLZ does not match exactly.
        if gemeinde_n:
            gem_c     = _canon_gem(gemeinde_n)
            row_gem_c = _canon_gem(row_gemeinde_n)
            if gem_c == row_gem_c:
                g_score = 1.0
            elif gem_c and (gem_c in row_gem_c or row_gem_c in gem_c):
                g_score = 0.9
            else:
                g_lev   = levenshtein(gem_c, row_gem_c)
                g_maxl  = max(len(gem_c), len(row_gem_c), 1)
                g_score = 1.0 - (g_lev / g_maxl)
            if g_score < _GEMEINDE_MIN_SCORE and not plz_match:
                continue   # wrong town and no matching PLZ → reject
        else:
            g_score = 0.5

        # Street: Levenshtein on the canonical form (hyphen/ß/abbreviations
        # unified) so that e.g. "St.-Anna-Str." and "Sankt Anna Straße" are
        # recognized as nearly identical.
        row_strasse_c = _canonical(row_strasse_n)
        lev     = levenshtein(strasse_c, row_strasse_c)
        maxl    = max(len(strasse_c), len(row_strasse_c), 1)
        s_score = 1.0 - (lev / maxl)

        if s_score < fuzzy_threshold:
            continue

        # House number
        hn_score = 0.0
        if hausnr_n and row['hausnummer']:
            row_hn = re.sub(r'\s+', '',
                (row['hausnummer'] + (row['hausnr_zusatz'] or '')).lower())
            if hausnr_n == row_hn:
                hn_score = 1.0
            elif (row_hn.startswith(hausnr_n)
                  and row_hn[len(hausnr_n):].isalpha()):
                # Only a letter suffix counts as a partial hit ("12" ->
                # "12a"). An additional digit ("1" -> "10", "12", "13") is
                # a DIFFERENT house number and must not get a bonus.
                hn_score = 0.5

        # Tie the house number bonus to the street quality: a matching number
        # on a clearly DIFFERENT street („Karl-Albert-Straße 1" for
        # „Karl-Marx-Straße 1") must not overtake the correct street without
        # that number. From 0.9 street similarity the bonus counts in full,
        # below that it decreases linearly, below 0.6 not at all.
        if s_score < 0.9:
            hn_score *= max(0.0, (s_score - 0.6) / 0.3)

        plz_score = 1.0 if plz_match else 0.0

        # Total score with dynamic weights. Base weights: street 0.45,
        # house number 0.25, Gemeinde 0.15, PLZ 0.15. If an input field is
        # missing, its weight is redistributed instead of capping the score:
        #   • no (confirmable) PLZ  -> weight goes to street/Gemeinde. This also
        #     applies if the DB row has no PLZ: a correct address is then not
        #     downgraded just because the data contains no PLZ.
        #   • no house number given -> weight goes to the street (it is then the
        #     decisive field; Gemeinde/PLZ only confirm and do NOT lift a weak
        #     street hit).
        # This way a clean street hit without house number reads "Exakt", while
        # a weak street hit stays "Unsicher".
        w_str, w_hn, w_gem, w_plz = 0.45, 0.25, 0.15, 0.15
        if not plz_usable:
            w_str += 0.10
            w_gem += 0.05
            w_plz = 0.0
        if not hausnr_n:
            w_str += w_hn
            w_hn = 0.0
            if w_gem > 0.15:                 # revert the Gemeinde weight raised
                w_str += w_gem - 0.15        # earlier (for lack of PLZ) in favour
                w_gem = 0.15                 # of the street
        total = (s_score   * w_str
               + hn_score  * w_hn
               + g_score   * w_gem
               + plz_score * w_plz)

        rd = dict(row)
        rd['_hn_score'] = hn_score
        scored.append((total, rd))

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, row in scored[:max_results]:
        results.append({
            'score':       round(score, 4),
            'score_label': _score_label(score),
            'hinweis':     hinweis,
            'strasse':     row['strasse'],
            'hausnummer':  (row['hausnummer'] or '') + (row['hausnr_zusatz'] or ''),
            'hn_requested': bool(hausnr_n),
            'hn_score':    row.get('_hn_score', 0.0),
            'plz':         row['plz'] or '',
            'gemeinde':    row['gemeinde'] or '',
            'bundesland':  row['bundesland'],
            'x':           row['x'],
            'y':           row['y'],
            'epsg':        _fix_epsg(row['epsg']),
            'strasse_norm':  row['strasse_norm'] or '',
            'gemeinde_norm': row['gemeinde_norm'] or '',
        })
    return results


def street_representative(strasse_norm, gemeinde_norm, bundesland=None,
                          plz=None, hausnummer=''):
    """Return a representative point for a street when the requested house
    number does not exist (or none was given). Instead of an arbitrary house
    number of the street (previous behaviour):

      * house number given but not present → numerically nearest house
        number of the street (the smaller one on a tie), result
        'nearest' with the chosen house number.
      * no house number given → the address point closest to the centroid
        of all points of the street ('center'); this guarantees the point
        lies on the street, even if it is curved.

    Returns dict(kind, x, y, epsg, hausnummer, count) or None."""
    if not strasse_norm:
        return None
    sql    = ("SELECT hausnummer, hausnr_zusatz, x, y, epsg FROM addresses "
              "WHERE strasse_norm = ?")
    params = [strasse_norm]
    if gemeinde_norm:
        sql += " AND gemeinde_norm = ?"
        params.append(gemeinde_norm)
    elif plz:
        sql += " AND plz = ?"
        params.append(plz)
    if bundesland:
        sql += " AND bundesland = ?"
        params.append(bundesland)
    sql += " LIMIT 5000"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    m = re.match(r'\d+', (hausnummer or '').strip())
    if m:
        want = int(m.group(0))
        best = None
        for r in rows:
            hm = re.match(r'\d+', (r['hausnummer'] or '').strip())
            if not hm:
                continue
            hn   = int(hm.group(0))
            key  = (abs(hn - want), hn)
            if best is None or key < best[0]:
                best = (key, r)
        if best is not None:
            r = best[1]
            return {'kind': 'nearest', 'x': r['x'], 'y': r['y'],
                    'epsg': _fix_epsg(r['epsg']),
                    'hausnummer': (r['hausnummer'] or '') + (r['hausnr_zusatz'] or ''),
                    'count': len(rows)}

    cx = sum(r['x'] for r in rows) / len(rows)
    cy = sum(r['y'] for r in rows) / len(rows)
    r  = min(rows, key=lambda r: (r['x'] - cx) ** 2 + (r['y'] - cy) ** 2)
    return {'kind': 'center', 'x': r['x'], 'y': r['y'],
            'epsg': _fix_epsg(r['epsg']),
            'hausnummer': (r['hausnummer'] or '') + (r['hausnr_zusatz'] or ''),
            'count': len(rows)}


def _fix_epsg(v):
    try:
        v = int(v)
        return 25800 + v if v < 100 else v
    except (TypeError, ValueError):
        return 25832


def _score_label(score):
    if score >= 0.92: return 'Exakt'
    elif score >= 0.75: return 'Sehr gut'
    elif score >= 0.55: return 'Gut'
    elif score >= 0.40: return 'Unsicher'
    else: return 'Schwach'


def locate_locality(plz='', gemeinde='', bundesland=None):
    """Return a representative point for a partial input WITHOUT a (matching)
    street: the centroid (mean) of all address points of a PLZ or a Gemeinde.
    This sets a point even for a bare PLZ or place/town input instead of a
    NULL geometry; the coarseness is expressed via accuracy_level=2.

    Rows are grouped by EPSG (and, for a place, additionally by the official
    Gemeinde name) and the largest group is taken, so that neither UTM zones
    (25832/25833) nor same-named places with different spellings get mixed.

    Returns dict (x, y, epsg, bundesland, level, label, count) or None.
    """
    plz_n      = (plz or '').strip()
    gemeinde_n = _core_gemeinde(gemeinde) if gemeinde else ''
    if not plz_n and not gemeinde_n:
        return None
    conn = get_connection()
    try:
        # PLZ first — unique nationwide and usually smaller in area than a place.
        if plz_n:
            row = conn.execute(
                "SELECT AVG(x) AS ax, AVG(y) AS ay, epsg, "
                "       MAX(bundesland) AS bundesland, COUNT(*) AS c "
                "FROM addresses WHERE plz = ? "
                "GROUP BY epsg ORDER BY c DESC LIMIT 1", (plz_n,)).fetchone()
            if row and row['c'] and row['ax'] is not None:
                return {'x': row['ax'], 'y': row['ay'],
                        'epsg': _fix_epsg(row['epsg']), 'bundesland': row['bundesland'],
                        'level': 3, 'label': 'PLZ-Gebiet (Schwerpunkt)', 'count': row['c']}
        if gemeinde_n:
            targets = _gemeinde_targets(conn, gemeinde_n, bundesland,
                                        raw=gemeinde, plz=plz_n)
            if targets:
                gn, bl, _n = targets[0]
                row = conn.execute(
                    "SELECT cx, cy, epsg, bundesland, n FROM gemeinde_index "
                    "WHERE gemeinde_norm = ? AND bundesland = ? "
                    "ORDER BY n DESC LIMIT 1", (gn, bl)).fetchone()
                if row and row['n']:
                    return {'x': row['cx'], 'y': row['cy'],
                            'epsg': _fix_epsg(row['epsg']), 'bundesland': row['bundesland'],
                            'level': 2, 'label': 'Ort/Gemeinde (Schwerpunkt)', 'count': row['n']}
            sql = ("SELECT AVG(x) AS ax, AVG(y) AS ay, epsg, "
                   "       MAX(bundesland) AS bundesland, COUNT(*) AS c "
                   "FROM addresses WHERE gemeinde_norm = ?")
            params = [gemeinde_n]
            if bundesland:
                sql += " AND bundesland = ?"
                params.append(bundesland)
            sql += " GROUP BY gemeinde, epsg ORDER BY c DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            if row and row['c'] and row['ax'] is not None:
                return {'x': row['ax'], 'y': row['ay'],
                        'epsg': _fix_epsg(row['epsg']), 'bundesland': row['bundesland'],
                        'level': 2, 'label': 'Ort/Gemeinde (Schwerpunkt)', 'count': row['c']}
        return None
    finally:
        conn.close()
