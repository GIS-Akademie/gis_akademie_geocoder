# -*- coding: utf-8 -*-
"""
Background worker for importing address text files (HK-DE format) into the SQLite DB.

HK-DE standard format (20 columns, 0-based, NRW/Hessen/others):
  0  nba  4  land  8  kreis  10  gmd  14  str  15  hnr  16  adz  17  zone  18  ostwert  19  nordwert

Berlin format (25 columns, first column EMPTY → all indices +1):
  0  (empty)  1  oid  5  land  9  kreis  11  gmd  15  str  16  hnr  17  adz  18  zone
  19  ostwert  20  nordwert  21  postplz

The shared write path of all import workers (one transaction, end sequence,
plausibility check against a strong decline) is AddressSink at the end of the
module-level helpers.
"""


import contextlib
import codecs
import os
import re
import sqlite3

from qgis.PyQt.QtCore import QObject, pyqtSignal

from . import local_geocoder as _lc
from .local_geocoder import init_db, normalize, _core_gemeinde
from .osm_convert import split_housenumbers
from .plz_resolver import build_resolver_logged


def _normalize_epsg(raw):
    """Normalizes the short EPSG form (e.g. '32' → 25832, '33' → 25833)."""
    try:
        v = int(raw)
        if v < 100:          # short form: zone number only
            return 25800 + v
        return v
    except ValueError:
        return 25832


# ── Column maps ──────────────────────────────────────────────────────────────

# HK-DE without PLZ: 20-column standard (NRW, Hessen, Rheinland-Pfalz) and the
# 26-column variants without PLZ (Sachsen-Anhalt with empty first column,
# Baden-Württemberg) — identical column positions.
_COL_OHNE_PLZ = {
    'bundesland_name': 4,
    'kreis':           8,
    'gemeinde':        10,   # municipality, possibly with ', Stadt' suffix
    'plz':             -1,   # not available
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

# HK-DE with PLZ in column 20: 24 columns (Sachsen, Thüringen, …) and
# 26 columns (Brandenburg) — identical column positions.
_COL_MIT_PLZ = dict(_COL_OHNE_PLZ, plz=20)

# Bayern format HK-BY 5.0 (24 columns, same layout as Sachsen). Differences:
# header row present (skipped), land='Bayern', and gmd may be empty -> then
# postonm (column 21) as municipality fallback.
# Source of the currently integrated data: OpenStreetMap (ODbL), not official.
_COL_BAYERN = dict(_COL_MIT_PLZ, gemeinde_fallback=21)

# Column indices of the Saarland format (20 columns, first column empty, own layout)
# LAN[3] is a numeric key ('10' = Saarland), NOT a plain-text Bundesland name
# -> bundesland_name=-3 as a marker: set 'Saarland' as a fixed value (like gemeinde=-2 for Berlin)
# ONM[15] is the place name in plain text (e.g. 'Illingen') -> used as gemeinde
# XCOORD[11]/YCOORD[12] use a COMMA as decimal separator, XCOORD has the
# UTM zone embedded as a prefix (e.g. '32360468,695' -> zone 32, X=360468.695)
# -> separate parsing path in the import (see _parse_saarland_xy)
_COL_SAARLAND = {
    'bundesland_name': -3,  # FIXED 'Saarland'
    'kreis':           5,   # KRS (district key, numeric)
    'gemeinde':        15,  # ONM - place name in plain text
    'plz':             14,  # PLZ directly available
    'strasse':         13,  # STN
    'hausnummer':      9,   # HNR
    'hausnr_zusatz':   10,  # ADZ
    'epsg':            -4,  # zone is in the XCOORD prefix, see _parse_saarland_xy
    'x':               11,  # XCOORD (comma decimal, with zone prefix)
    'y':               12,  # YCOORD (comma decimal)
}

# Column indices of the Berlin format (25 columns, first column empty)
# ;oid;qua;landschl;land(=Berlin);0;;00;;kreisschl;kreis(Bezirk);ottschl;ott(Ortsteil);strschl;str;hnr;adz;zone;ost;nord;plz;...
_COL_BERLIN = {
    'bundesland_name': 4,   # 'Berlin'
    'kreis':           10,  # borough (Bezirk), e.g. 'Friedrichshain-Kreuzberg'
    'gemeinde':        -2,  # FIXED 'Berlin' — set in code, not read from a column
    'plz':             20,  # PLZ directly available
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

# Format table used by the import and the preview:
# format key -> (column map, label, minimum number of columns of a data line)
_FORMATS = {
    'hkde':               (_COL_OHNE_PLZ, 'HK-DE-Standard (20-spaltig)', 20),
    'saarland':           (_COL_SAARLAND, 'Saarland (eigenes 20-spaltiges Format)', 20),
    'berlin':             (_COL_BERLIN, 'Berlin (HK-DE 25-spaltig)', 20),
    'sachsen_anhalt':     (_COL_OHNE_PLZ, 'Sachsen-Anhalt (HK-DE 26-spaltig, ohne PLZ)', 20),
    'baden_wuerttemberg': (_COL_OHNE_PLZ, 'Baden-Württemberg (HK-DE 26-spaltig, ohne PLZ)', 20),
    'brandenburg':        (_COL_MIT_PLZ, 'Brandenburg (HK-DE 26-spaltig, mit PLZ)', 21),
    'sachsen':            (_COL_MIT_PLZ,
                           'HK-DE 24-spaltig (Sachsen/Thüringen/MV/Niedersachsen u.a.)', 21),
    'bayern':             (_COL_BAYERN, 'Bayern (HK-BY 24-spaltig, Quelle: OpenStreetMap/ODbL)', 21),
}

# Number of data lines inspected by the format detection
_DETECT_LINES = 200


def _parse_saarland_xy(xcoord_raw, ycoord_raw):
    """Parses Saarland coordinates: comma as decimal separator,
    XCOORD contains the 2-digit UTM zone as a prefix.
    '32360468,695' -> zone='32', X=360468.695
    '5473701,597'  -> Y=5473701.597 (no zone prefix for Y)
    Returns (zone, x, y) or raises ValueError (also for a zone prefix other
    than 32/33 — the row is then skipped instead of being stored in a
    nonsensical coordinate system).
    """
    xcoord = xcoord_raw.strip().replace(',', '.')
    ycoord = ycoord_raw.strip().replace(',', '.')
    zone = xcoord[:2]
    if zone not in ('32', '33'):
        raise ValueError(f'unbekannte UTM-Zone {zone!r}')
    x = float(xcoord[2:])
    y = float(ycoord)
    return zone, x, y


BATCH_SIZE = 5000


def _n(x):
    """Integer with German thousands separator: 4509537 -> '4.509.537'."""
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


# ── Encoding / delimiter / format detection ─────────────────────────────────

# Bytes that are undefined in cp1252 — if one occurs, the file is latin-1
_CP1252_UNDEFINED = (b'\x81', b'\x8d', b'\x8f', b'\x90', b'\x9d')
_SCAN_CACHE = {}          # (path, size, mtime_ns) -> (encoding, n_lines)
_SCAN_CHUNK = 1 << 20


def _scan_file(filepath):
    """Reads the WHOLE file once (in chunks) and returns (encoding, n_lines).

    Encoding: BOM-aware (UTF-8 / UTF-16); otherwise strict UTF-8 over the
    entire file — a single invalid byte anywhere (e.g. an 'ü' in cp1252 after
    20 KB of pure ASCII) falls back to cp1252, or to latin-1 if the file
    contains bytes that are undefined in cp1252. The result is cached per
    path/size/mtime because import, delimiter/format detection and the
    preview all need it."""
    try:
        st = os.stat(filepath)
    except OSError:
        return 'utf-8', 0
    ck = (os.path.abspath(filepath), st.st_size, st.st_mtime_ns)
    hit = _SCAN_CACHE.get(ck)
    if hit is not None:
        return hit
    n_lines = 0
    with open(filepath, 'rb') as f:
        head = f.read(4)
        if head.startswith(codecs.BOM_UTF8):
            enc = 'utf-8-sig'
        elif head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
            enc = 'utf-16'
        else:
            enc = None
        f.seek(0)
        dec = codecs.getincrementaldecoder('utf-8')('strict') if enc is None else None
        utf8_ok = enc is None
        undefined_1252 = False
        while True:
            chunk = f.read(_SCAN_CHUNK)
            if not chunk:
                break
            n_lines += chunk.count(b'\n')
            if enc is not None:
                continue
            if utf8_ok:
                try:
                    dec.decode(chunk)
                except UnicodeDecodeError:
                    utf8_ok = False
            if not utf8_ok and not undefined_1252:
                undefined_1252 = any(b in chunk for b in _CP1252_UNDEFINED)
        if enc is None:
            if utf8_ok:
                try:
                    dec.decode(b'', final=True)
                except UnicodeDecodeError:
                    utf8_ok = False
            if utf8_ok:
                enc = 'utf-8'
            else:
                enc = 'latin-1' if undefined_1252 else 'cp1252'
    # (UTF-16: 0x0A bytes are counted — an estimate for the progress bar only)
    if len(_SCAN_CACHE) > 32:
        _SCAN_CACHE.clear()
    _SCAN_CACHE[ck] = (enc, n_lines)
    return enc, n_lines


def _detect_encoding(filepath):
    """Text encoding of an address file (see _scan_file)."""
    return _scan_file(filepath)[0]


def _open_text(filepath):
    """Opens the file as text in the detected encoding. errors='replace' only
    protects against a file that changed after the detection."""
    return open(filepath, encoding=_detect_encoding(filepath), errors='replace', newline='')


def _detect_delimiter(filepath):
    """Detects the field delimiter from the first data line: ';' (HK-DE
    standard) or ',' (CSV variant such as Hessen's 'Hauskoordinaten ohne
    Postalische Angaben'). Decided by frequency in the first non-empty
    record."""
    with contextlib.suppress(OSError):
        with _open_text(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                return ';' if line.count(';') >= line.count(',') else ','
    return ';'


def _iter_parts(fileobj, delim):
    """Yields the field list per record. For ';' the existing fast path
    (split). For ',' real CSV parsing (csv module) — removes quotes and
    handles embedded commas correctly."""
    if delim == ',':
        import csv
        for parts in csv.reader(fileobj, delimiter=','):
            yield parts
    else:
        for line in fileobj:
            line = line.strip()
            yield line.split(';') if line else []


def _is_header(parts):
    return bool(parts) and parts[0].strip().lower() in ('nba', 'kennung')


def _detect_format(filepath):
    """Detects the HK-DE variant from the header and the first data lines
    (up to _DETECT_LINES, so that an empty PLZ in the FIRST line does not
    decide the format alone).
    - Saarland:        first column empty, fewer than 22 fields
    - Berlin:          first column empty, 26 fields, land 'Berlin' (or PLZ present)
    - Sachsen-Anhalt:  first column empty, 26 fields, no PLZ
    - Brandenburg:     26 columns, first column NOT empty, PLZ in column 20 present
    - Baden-Württemberg: 26 columns, first column NOT empty, no PLZ
    - Sachsen/Thüringen: 24 columns, PLZ in column 20 (possibly empty)
    - Bayern:          24 columns, land 'Bayern' (HK-BY)
    - Standard:        20 columns (NRW, Hessen, Rheinland-Pfalz)
    """
    delim = _detect_delimiter(filepath)
    header_cols = 0
    data = []
    try:
        with _open_text(filepath) as f:
            for parts in _iter_parts(f, delim):
                if not parts:
                    continue
                if _is_header(parts):
                    header_cols = len(parts)
                    continue
                data.append(parts)
                if len(data) >= _DETECT_LINES:
                    break
    except OSError:
        return 'hkde'
    if not data:
        return 'brandenburg' if header_cols >= 26 else 'hkde'
    first = data[0]
    has_plz = any(len(p) > 20 and p[20].strip() for p in data)
    land = _canonical_bundesland(first[4].strip()) if len(first) > 4 else ''
    if first[0].strip() == '':
        # First column empty: Saarland (20 fields), Berlin or Sachsen-Anhalt (26 fields).
        if len(first) < 22:
            return 'saarland'
        if land == 'Berlin':
            return 'berlin'
        if land:
            # a named state other than Berlin: municipality in column 10
            return 'brandenburg' if has_plz else 'sachsen_anhalt'
        return 'berlin' if has_plz else 'sachsen_anhalt'
    if len(first) >= 26:
        return 'brandenburg' if has_plz else 'baden_wuerttemberg'
    if len(first) >= 24:
        # HK-BY and Sachsen have identical columns — distinguished by the
        # plain-text Bundesland name in column 4.
        return 'bayern' if land == 'Bayern' else 'sachsen'
    return 'hkde'


def _canonical_bundesland(name):
    """Normalizes official Bundesland names to the short form that is
    also used in the UI combo box and in the PLZ mapping,
    e.g. 'Freistaat Sachsen' -> 'Sachsen', 'Freistaat Bayern' -> 'Bayern'.
    """
    replacements = {
        'Freistaat Sachsen':   'Sachsen',
        'Freistaat Bayern':    'Bayern',
        'Freistaat Thüringen': 'Thüringen',
        'Land Berlin':         'Berlin',
        'Land Brandenburg':    'Brandenburg',
        'Land Hessen':         'Hessen',
        'Freie und Hansestadt Hamburg': 'Hamburg',
        'Freie Hansestadt Bremen':      'Bremen',
    }
    return replacements.get(name, name)


# ── Municipality names and house numbers ─────────────────────────────────────

# Status designations in the gmd column (municipality name) that users
# typically do NOT type when geocoding (e.g. 'Chemnitz' instead of
# 'Stadt Chemnitz'). They are removed during import — as a prefix
# ('Stadt Chemnitz') as well as a suffix after a comma ('Güstrow, Barlachstadt').
_GEMEINDE_PREFIXES = (
    'Kreisfreie Stadt ',
    'Große Kreisstadt ',
    'Hanse- und Universitätsstadt ',   # Rostock
    'Universitäts- und Hansestadt ',   # Greifswald
    'Landeshauptstadt ',               # Schwerin
    'Barlachstadt ',                   # Güstrow
    'Vier-Tore-Stadt ',                # Neubrandenburg
    'Reuterstadt ',                    # Stavenhagen
    'Hansestadt ',                     # Wismar, Stralsund, Anklam, Demmin, Lübeck …
    'Stadtgemeinde ',                  # Bremen, Bremerhaven
    'Stadt ',
)

# Suffixes in the gmd column (Sachsen-Anhalt, Mecklenburg-Vorpommern,
# Niedersachsen) that users typically do not type, e.g. 'Wetterzeube' instead
# of 'Wetterzeube, Stadt'. Every status word of the prefix list is also
# removed as a suffix. Parenthetical additions like '(Milde)' or '(Saale)' are
# part of the official town name and are NOT removed — 'Kalbe (Milde)' stays.
_GEMEINDE_SUFFIXES = tuple(sorted(
    {', ' + p.strip() for p in _GEMEINDE_PREFIXES} | {', Flecken'},
    key=len, reverse=True))


_HAUSNUMMER_RE = re.compile(r'^\d{1,4}$')
_LONG_NUMBER_RE = re.compile(r'\d{5,}')


def _is_plausible_hausnummer(hnr):
    """Checks whether a house number is plausible (1-4 digits).
    Filters out parcel/forest-division IDs that appear as pseudo house
    numbers for forest plots/parcels in some Bundesländer
    (e.g. Baden-Württemberg), e.g. '84068997'.
    """
    return bool(_HAUSNUMMER_RE.match(hnr))


def _hausnummer_rows(hnr_raw, zusatz=''):
    """House number field -> list of (hausnummer, zusatz) for the rows to store.

    '12' -> [('12', zusatz)]; '' -> [('', zusatz)] (address without number);
    '12a' / '12 a' -> [('12', 'a')], '12-14' -> [('12', '')],
    '12;14' -> [('12', ''), ('14', '')] (split like the OSM/ALKIS paths; a
    filled suffix field takes precedence over a suffix in the number).
    Implausible pseudo numbers (more than 4 digits, e.g. parcel IDs) and values
    without a leading digit -> [] (row is skipped)."""
    hnr_raw = (hnr_raw or '').strip()
    zusatz = (zusatz or '').strip()
    if not hnr_raw or _is_plausible_hausnummer(hnr_raw):
        return [(hnr_raw, zusatz)]
    if _LONG_NUMBER_RE.search(hnr_raw):
        return []
    return [(n, zusatz or adz) for n, adz in split_housenumbers(hnr_raw)]


def _clean_gemeinde(name):
    """Removes leading/trailing administrative designations from municipality names."""
    name = (name or '').strip()
    for prefix in _GEMEINDE_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
            break
    for suffix in _GEMEINDE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[:-len(suffix)]
            break
    return name.strip()


# ── Shared write path of all import workers ─────────────────────────────────

CANCEL_MSG = 'Import abgebrochen – der bisherige Datenbestand bleibt erhalten.'
KEEP_MSG = 'Der bisherige Datenbestand bleibt erhalten.'

# Plausibility check against a strong decline (e.g. truncated download):
# only for states with at least this many addresses …
SHRINK_MIN_PREV = 1000
# … and if the new data contain less than this fraction of the old count.
SHRINK_RATIO = 0.5


def no_addresses_msg(name=None):
    where = f'In der Datei {name}' if name else 'In der Datei'
    return f'{where} wurden keine Adressen gefunden – der bisherige Datenbestand bleibt erhalten.'


def shrink_msg(n_ok, prev):
    return (f'Die neuen Daten enthalten nur {_n(n_ok)} statt bisher {_n(prev)} Adressen – '
            'vermutlich ist der Download unvollständig. Der bisherige Datenbestand bleibt '
            'erhalten. Mit „Auch ohne neue Daten erneut laden“ lassen sich die neuen Daten '
            'trotzdem übernehmen.')




class AddressSink:
    """Writes the addresses of ONE import (one or several files) into the DB.

    Everything happens in ONE SQLite transaction: the optional DELETE of the
    state's rows (replace=True), all INSERTs and the end sequence (PLZ meta,
    optional municipality/locality index). finish() commits once; any error,
    cancel, 'no addresses' or the shrink guard rolls back everything, so the
    state's existing data stay untouched.

    Usage:
        sink = AddressSink(...)
        try:
            sink.start(bundesland)            # DELETE (replace=True)
            sink.add(...) per address
            ok, msg = sink.finish(resolver)   # commit or rollback
        except Exception:
            sink.rollback(); raise
        finally:
            sink.close()
    """

    INSERT_SQL = """
        INSERT INTO addresses
          (bundesland, kreis, gemeinde, plz, strasse, hausnummer,
           hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm, plz_herkunft)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    def __init__(self, replace=True, rebuild_lookup=True, allow_shrink=False,
                 log=None, detail=None, db_path=None):
        self.replace = replace
        self.rebuild_lookup = rebuild_lookup
        self.allow_shrink = allow_shrink
        self._log = log or (lambda m: None)
        self._detail = detail or (lambda m: None)
        self.conn = sqlite3.connect(db_path or _lc.DB_PATH)
        init_db(self.conn)
        self.batch = []
        self.n_ok = 0
        self.states = {}          # bundesland -> {'prev': int, 'n': int}
        self._norm_cache = {}     # street -> normalize(street)
        self._gem_cache = {}      # municipality -> _core_gemeinde(municipality)

    # -- transaction ------------------------------------------------------

    def start(self, bundesland):
        """Registers a state of this import; with replace=True its rows are
        counted (shrink guard) and deleted — inside the transaction."""
        if bundesland in self.states:
            return
        prev = 0
        if self.replace:
            prev = self.conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland = ?",
                                     (bundesland,)).fetchone()[0]
            self._log(f'Die bisherigen Adressen für {bundesland} werden ersetzt.')
            self.conn.execute("DELETE FROM addresses WHERE bundesland = ?", (bundesland,))
        self.states[bundesland] = {'prev': prev, 'n': 0}

    def add(self, bundesland, kreis, gemeinde, plz, strasse, hausnummer, hausnr_zusatz,
            epsg, x, y, herkunft=None):
        if bundesland not in self.states:
            self.start(bundesland)
        sn = self._norm_cache.get(strasse)
        if sn is None:
            sn = self._norm_cache[strasse] = normalize(strasse)
        gn = self._gem_cache.get(gemeinde)
        if gn is None:
            gn = self._gem_cache[gemeinde] = _core_gemeinde(gemeinde)
        self.batch.append((bundesland, kreis, gemeinde, plz, strasse, hausnummer,
                           hausnr_zusatz, epsg, x, y, sn, gn, herkunft))
        self.n_ok += 1
        self.states[bundesland]['n'] += 1
        if len(self.batch) >= BATCH_SIZE:
            self.flush()

    def flush(self):
        # NO commit per batch — the whole import is ONE transaction.
        if self.batch:
            self.conn.executemany(self.INSERT_SQL, self.batch)
            self.batch = []

    def rollback(self):
        self.batch = []
        with contextlib.suppress(sqlite3.Error):
            self.conn.rollback()

    def close(self):
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    def finish(self, resolver=None, no_addresses=None):
        """End sequence: checks, PLZ message/meta, optional index, ONE commit.
        Returns (True, None) after the commit or (False, message) after a
        rollback."""
        self.flush()
        if self.n_ok == 0:
            # Empty or wrong file (e.g. maintenance page instead of data):
            # replace nothing — otherwise the state's data would be deleted
            self.rollback()
            return False, no_addresses or no_addresses_msg()
        if self.replace and not self.allow_shrink:
            for bl, st in self.states.items():
                if st['prev'] >= SHRINK_MIN_PREV and st['n'] < st['prev'] * SHRINK_RATIO:
                    self.rollback()
                    self._detail(f'{bl}: {st["n"]} statt bisher {st["prev"]} Adressen – '
                                 'Import zurückgerollt.')
                    return False, shrink_msg(st['n'], st['prev'])
        if resolver is not None:
            st = resolver.stats()
            if st['matched'] or st['missed']:   # nothing to fill in → no message
                self._log(f"PLZ ergänzt für {_n(st['matched'])} Adressen"
                          + (f"; {_n(st['missed'])} liegen außerhalb der PLZ-Flächen."
                             if st['missed'] else '.'))
        info = _lc.resolver_info(resolver)
        for bl in self.states:
            _lc.record_plz_meta(self.conn, bl, info, self.replace)
        # Municipality/locality index for the new data (part of the same
        # transaction), then ONE atomic commit including the DELETE.
        if self.rebuild_lookup:
            _lc.rebuild_lookup_tables(self.conn, self._log, bundeslaender=list(self.states))
        self.conn.commit()
        return True, None


def _as_list(filepaths):
    return [filepaths] if isinstance(filepaths, str) else list(filepaths)


class DbImportWorker(QObject):

    progress  = pyqtSignal(int, int, str)   # read so far, total, status
    log       = pyqtSignal(str)             # log for the user
    detail    = pyqtSignal(str)             # technical details (QGIS message log)
    finished  = pyqtSignal(bool, str)       # success, message

    def __init__(self, filepaths, bundesland_override=None, replace=True,
                 rebuild_lookup=True, announce=True, allow_shrink=False):
        """filepaths: one file (str) or a list — all files are imported in ONE
        transaction (each with its own format/encoding/delimiter detection).
        bundesland_override: None = read the state from the file.
        replace: delete the state's existing addresses (inside the transaction).
        rebuild_lookup: rebuild the municipality/locality index afterwards
        (2–3 minutes for large DBs; False for the automatic download).
        announce=False: called from the automatic download — file name and
        Bundesland go to the detail channel only.
        allow_shrink: accept new data with far fewer addresses than before."""
        super().__init__()
        self.filepaths          = _as_list(filepaths)
        self.filepath           = self.filepaths[0] if self.filepaths else ''
        self.bundesland_override = bundesland_override
        self._cancelled         = False
        self.replace            = replace
        self.rebuild_lookup     = rebuild_lookup
        self.announce           = announce
        self.allow_shrink       = allow_shrink

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            import traceback
            self.detail.emit(traceback.format_exc())
            self.finished.emit(False, f'Import fehlgeschlagen: {e}. {KEEP_MSG}')

    def _run(self):
        if not self.filepaths:
            self.finished.emit(False, no_addresses_msg())
            return
        say = self.log if self.announce else self.detail
        total = sum(_scan_file(p)[1] for p in self.filepaths)
        self.detail.emit(f'Geschätzte Zeilenzahl: {_n(total)}')

        sink = AddressSink(self.replace, self.rebuild_lookup, self.allow_shrink,
                           self.log.emit, self.detail.emit)
        try:
            # PLZ fill-in from the active postcode areas
            resolver = build_resolver_logged(log=self.log.emit, detail=self.detail.emit)
            counters = {'lines': 0, 'skip': 0}
            multi = len(self.filepaths) > 1
            for path in self.filepaths:
                n_before = sink.n_ok
                if not self._import_file(path, sink, resolver, counters, total, say):
                    sink.rollback()     # discard the deletion + inserts so far
                    self.finished.emit(False, CANCEL_MSG)
                    return
                if sink.n_ok == n_before:
                    sink.rollback()
                    self.finished.emit(False, no_addresses_msg(
                        os.path.basename(path) if multi else None))
                    return
            ok, msg = sink.finish(resolver)
            if not ok:
                self.finished.emit(False, msg)
                return
        except Exception:
            sink.rollback()     # discard DELETE + inserts so far completely
            raise               # run() reports the error; existing data is kept
        finally:
            sink.close()

        self.progress.emit(counters['lines'], total, 'Fertig.')
        msg = f'Import abgeschlossen: {_n(sink.n_ok)} Adressen importiert'
        if counters['skip']:
            msg += f' ({_n(counters["skip"])} Zeilen ohne verwertbare Adresse übersprungen)'
        self.finished.emit(True, msg + '.')

    def _import_file(self, path, sink, resolver, counters, total, say):
        """Imports one file into the sink. Returns False on cancel."""
        say.emit(f'Lese {os.path.basename(path)} …')
        self.detail.emit(f'Datei: {path}')

        fmt = _detect_format(path)
        col, fmt_label, min_cols = _FORMATS.get(fmt, _FORMATS['hkde'])
        self.detail.emit(f'Format erkannt: {fmt_label}')

        # Determine the Bundesland from the first data line if not given
        bundesland = self.bundesland_override or _detect_bundesland(path, col)
        say.emit(f'Bundesland: {bundesland}')
        sink.start(bundesland)

        self.detail.emit(f'Encoding: {_detect_encoding(path)}')
        delim = _detect_delimiter(path)
        if delim == ',':
            self.detail.emit('Trennzeichen: Komma (CSV mit Anführungszeichen).')

        c_kreis, c_str, c_hnr, c_adz = (col['kreis'], col['strasse'], col['hausnummer'],
                                        col['hausnr_zusatz'])
        c_plz, c_gem, c_epsg = col['plz'], col['gemeinde'], col['epsg']
        c_fb = col.get('gemeinde_fallback', -1)
        saarland = fmt == 'saarland'
        gem_cache = {}            # raw municipality -> cleaned (memoised per file)

        with _open_text(path) as f:
            for parts in _iter_parts(f, delim):
                if self._cancelled:
                    return False

                counters['lines'] += 1
                if counters['lines'] % 20000 == 0:
                    self.progress.emit(counters['lines'], total,
                                       f'{_n(sink.n_ok)} Adressen eingelesen …')

                if not parts or _is_header(parts):
                    continue   # empty line / header row (HK-BY and others)
                if len(parts) < min_cols:
                    counters['skip'] += 1
                    continue

                try:
                    if saarland:
                        # Saarland: comma decimal, zone as prefix in XCOORD
                        zone_sl, x, y = _parse_saarland_xy(parts[col['x']], parts[col['y']])
                    else:
                        x = float(parts[col['x']].replace(',', '.'))
                        y = float(parts[col['y']].replace(',', '.'))
                except (ValueError, IndexError):
                    counters['skip'] += 1
                    continue

                strasse = parts[c_str].strip()
                if not strasse:
                    counters['skip'] += 1
                    continue

                hnr_raw = parts[c_hnr].strip()
                if hnr_raw and not _is_plausible_hausnummer(hnr_raw):
                    # Implausible pseudo house number (e.g. parcel/forest ID
                    # like '84068997' in Baden-Württemberg) -> skip
                    counters['skip'] += 1
                    continue

                # gemeinde=-2 = Berlin special case: fixed city name instead of a column
                if c_gem == -2:
                    gemeinde_val = bundesland  # e.g. 'Berlin'
                else:
                    raw = parts[c_gem].strip()
                    # HK-BY: gmd may be empty -> use postonm as fallback
                    if not raw and 0 <= c_fb < len(parts):
                        raw = parts[c_fb].strip()
                    gemeinde_val = gem_cache.get(raw)
                    if gemeinde_val is None:
                        gemeinde_val = gem_cache[raw] = _clean_gemeinde(raw)
                plz_val = parts[c_plz].strip() if 0 <= c_plz < len(parts) else ''
                if c_epsg == -4:
                    epsg_val = _normalize_epsg(zone_sl)
                else:
                    epsg_val = _normalize_epsg(parts[c_epsg].strip())

                herkunft = None
                if not plz_val and resolver is not None:
                    plz_val = resolver.lookup(x, y, epsg_val)
                    herkunft = 'gebiet' if plz_val else None

                sink.add(bundesland, parts[c_kreis].strip(), gemeinde_val, plz_val, strasse,
                         hnr_raw, parts[c_adz].strip(), epsg_val, x, y, herkunft)
        return True

    @staticmethod
    def preview_file(filepath, n_lines=3):
        """Returns the first n data lines as a list of dicts.
        Format-aware: picks the matching column map (Standard/Sachsen/Berlin)
        based on the detected column count and returns canonical Bundesland names.
        """
        fmt = _detect_format(filepath)
        col, _label, min_cols = _FORMATS.get(fmt, _FORMATS['hkde'])
        results = []
        delim = _detect_delimiter(filepath)
        with contextlib.suppress(OSError):
            with _open_text(filepath) as f:
                for parts in _iter_parts(f, delim):
                    if len(parts) < min_cols:
                        continue
                    if col['bundesland_name'] == -3:
                        bl = 'Saarland'
                    else:
                        bl = parts[col['bundesland_name']].strip()
                    # skip the header row: contains column names instead of data
                    if bl.lower() in ('land', 'bundesland', 'bl', 'state', '') or _is_header(parts):
                        continue
                    if col['gemeinde'] == -2:
                        gemeinde_val = _canonical_bundesland(bl)
                    else:
                        gemeinde_val = _clean_gemeinde(parts[col['gemeinde']].strip())
                        if not gemeinde_val:
                            fb = col.get('gemeinde_fallback', -1)
                            if 0 <= fb < len(parts):
                                gemeinde_val = _clean_gemeinde(parts[fb].strip())
                    results.append({
                        'bundesland': _canonical_bundesland(bl),
                        'gemeinde':   gemeinde_val,
                        'strasse':    parts[col['strasse']].strip(),
                        'hausnummer': parts[col['hausnummer']].strip(),
                        'x':          parts[col['x']].strip(),
                        'y':          parts[col['y']].strip(),
                    })
                    if len(results) >= n_lines:
                        break
        return results


def _detect_bundesland(filepath, col):
    """Bundesland from the first data line (plain-text land column)."""
    if col['bundesland_name'] == -3:
        return 'Saarland'
    delim = _detect_delimiter(filepath)
    idx = col['bundesland_name']
    with contextlib.suppress(OSError):
        with _open_text(filepath) as f:
            for parts in _iter_parts(f, delim):
                if len(parts) > idx:
                    bl = parts[idx].strip()
                    if bl and bl.lower() not in ('land', 'bundesland', 'bl'):
                        return _canonical_bundesland(bl)
    return 'Unbekannt'
