# -*- coding: utf-8 -*-
"""
Background worker for importing address text files (HK-DE format) into the SQLite DB.

HK-DE standard format (20 columns, 0-based, NRW/Hessen/others):
  0  nba  4  land  8  kreis  10  gmd  14  str  15  hnr  16  adz  17  zone  18  ostwert  19  nordwert

Berlin format (25 columns, first column EMPTY → all indices +1):
  0  (empty)  1  oid  5  land  9  kreis  11  gmd  15  str  16  hnr  17  adz  18  zone
  19  ostwert  20  nordwert  21  postplz
"""


import sqlite3
import os

from qgis.PyQt.QtCore import QObject, pyqtSignal

from . import local_geocoder as _lc
from .local_geocoder import init_db, normalize, _core_gemeinde
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


# Column indices of the HK-DE standard format (20 columns)
_COL_NRW = {
    'bundesland_name': 4,
    'kreis':           8,
    'gemeinde':        10,
    'plz':             -1,   # not included
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

# Column indices of the Sachsen format (24 columns, PLZ in column 20)
_COL_SACHSEN = {
    'bundesland_name': 4,
    'kreis':           8,
    'gemeinde':        10,
    'plz':             20,  # postplz directly available
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

# Column indices of the Bayern format HK-BY 5.0 (24 columns, same layout as
# Sachsen). Differences: header row present (skipped), land='Bayern',
# and gmd may be empty -> then postonm (column 21) as municipality fallback.
# Source of the currently integrated data: OpenStreetMap (ODbL), not official.
_COL_BAYERN = {
    'bundesland_name':   4,
    'kreis':             8,
    'gemeinde':         10,   # gmd
    'gemeinde_fallback': 21,  # postonm, if gmd is empty
    'plz':              20,   # postplz
    'strasse':          14,
    'hausnummer':       15,
    'hausnr_zusatz':    16,
    'epsg':             17,
    'x':                18,
    'y':                19,
}

# Column indices of the Brandenburg format (26 columns, like Sachsen + 2 extra columns)
_COL_BRANDENBURG = {
    'bundesland_name': 4,
    'kreis':           8,
    'gemeinde':        10,
    'plz':             20,  # postplz directly available
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

# Column indices of the Sachsen-Anhalt format (26 columns, first column empty, no PLZ)
# Same basic layout as Berlin, but PLZ columns (20-24) empty.
_COL_SACHSEN_ANHALT = {
    'bundesland_name': 4,
    'kreis':           8,    # district (Landkreis), e.g. 'Burgenlandkreis'
    'gemeinde':        10,   # municipality, e.g. 'Wetterzeube' (possibly with ', Stadt' suffix)
    'plz':             -1,   # not available
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

# Column indices of the Baden-Württemberg format (26 columns, like Brandenburg, PLZ empty)
_COL_BW = {
    'bundesland_name': 4,
    'kreis':           8,
    'gemeinde':        10,
    'plz':             -1,
    'strasse':         14,
    'hausnummer':      15,
    'hausnr_zusatz':   16,
    'epsg':            17,
    'x':               18,
    'y':               19,
}

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


def _parse_saarland_xy(xcoord_raw, ycoord_raw):
    """Parses Saarland coordinates: comma as decimal separator,
    XCOORD contains the 2-digit UTM zone as a prefix.
    '32360468,695' -> zone='32', X=360468.695
    '5473701,597'  -> Y=5473701.597 (no zone prefix for Y)
    Returns (zone, x, y) or raises ValueError.
    """
    xcoord = xcoord_raw.replace(',', '.')
    ycoord = ycoord_raw.replace(',', '.')
    zone = xcoord[:2]
    x = float(xcoord[2:])
    y = float(ycoord)
    return zone, x, y


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

# Backward compatibility: _COL points to the HK-DE standard (for preview_file)
_COL = _COL_NRW

BATCH_SIZE = 5000


def _n(x):
    """Integer with German thousands separator: 4509537 -> '4.509.537'."""
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


def _detect_delimiter(filepath):
    """Detects the field delimiter from the first data line: ';' (HK-DE
    standard) or ',' (CSV variant such as Hessen's 'Hauskoordinaten ohne
    Postalische Angaben'). Decided by frequency in the first non-empty
    record."""
    for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
        try:
            with open(filepath, encoding=enc, errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    return ';' if line.count(';') >= line.count(',') else ','
        except UnicodeDecodeError:
            continue
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


def _detect_format(filepath):
    """Detects the HK-DE variant from the first data line.
    - Berlin:          first column empty, 26 fields, column 20 (PLZ) filled
    - Sachsen-Anhalt:  first column empty, 26 fields, PLZ columns empty
    - Brandenburg:     26 columns, first column NOT empty, PLZ in column 20 filled
    - Baden-Württemberg: 26 columns, first column NOT empty, PLZ columns empty
    - Sachsen/Thüringen: 24 columns, PLZ in column 20 (possibly empty), first column not empty
    - Standard:        20 columns (NRW, Hessen, Rheinland-Pfalz)
    """
    delim = _detect_delimiter(filepath)
    for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
        try:
            with open(filepath, encoding=enc, errors='replace', newline='') as f:
                for parts in _iter_parts(f, delim):
                    if not parts:
                        continue
                    if parts[0].strip().lower() in ('nba', 'kennung'):
                        # header — column count determines the basic format
                        if len(parts) >= 26:
                            return 'brandenburg'
                        if len(parts) >= 24:
                            # 24 columns: Bayern (HK-BY) vs. Sachsen can only be
                            # decided from the first data line (land column).
                            continue
                        continue
                    if parts[0].strip() == '':
                        # First column empty: Saarland (20 fields), Berlin
                        # or Sachsen-Anhalt (both 26 fields).
                        if len(parts) < 22:
                            return 'saarland'
                        # Berlin vs. Sachsen-Anhalt via column 20 (postplz):
                        # Berlin has a PLZ there, Sachsen-Anhalt leaves it empty.
                        if len(parts) > 20 and parts[20].strip():
                            return 'berlin'
                        return 'sachsen_anhalt'
                    # data line: column count determines the format
                    if len(parts) >= 26:
                        if len(parts) > 20 and parts[20].strip():
                            return 'brandenburg'
                        return 'baden_wuerttemberg'
                    if len(parts) >= 24:
                        # HK-BY and Sachsen have identical columns — distinguished
                        # by the plain-text Bundesland name in column 4.
                        land = (_canonical_bundesland(parts[4].strip())
                                if len(parts) > 4 else '')
                        return 'bayern' if land == 'Bayern' else 'sachsen'
                    return 'hkde'
        except UnicodeDecodeError:
            continue
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


# Prefixes in the gmd column (municipality name) that users typically do NOT
# type when geocoding (e.g. 'Chemnitz' instead of 'Stadt Chemnitz').
# They are removed during import so that the city search matches exactly.
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
    'Stadt ',
)

# Suffixes in the gmd column (Sachsen-Anhalt) that users typically do not
# type, e.g. 'Wetterzeube' instead of 'Wetterzeube, Stadt'.
# Parenthetical additions like '(Milde)' or '(Saale)' are part of the official
# town name and are NOT removed — 'Kalbe (Milde)' stays as is.
_GEMEINDE_SUFFIXES = (
    ', Landeshauptstadt',
    ', Hansestadt',
    ', Stadt',
)


import re as _re
_HAUSNUMMER_RE = _re.compile(r'^\d{1,4}$')


def _is_plausible_hausnummer(hnr):
    """Checks whether a house number is plausible (1-4 digits).
    Filters out parcel/forest-division IDs that appear as pseudo house
    numbers for forest plots/parcels in some Bundesländer
    (e.g. Baden-Württemberg), e.g. '84068997'.
    """
    return bool(_HAUSNUMMER_RE.match(hnr))


def _clean_gemeinde(name):
    """Removes leading/trailing administrative designations from municipality names."""
    for prefix in _GEMEINDE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for suffix in _GEMEINDE_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name.strip()


class DbImportWorker(QObject):

    progress  = pyqtSignal(int, int, str)   # read so far, total, status
    log       = pyqtSignal(str)             # log for the user
    detail    = pyqtSignal(str)             # technical details (QGIS message log)
    finished  = pyqtSignal(bool, str)       # success, message

    def __init__(self, filepath, bundesland_override=None, replace=True,
                 rebuild_lookup=True, announce=True):
        super().__init__()
        self.filepath           = filepath
        self.bundesland_override = bundesland_override  # None = read from file
        self._cancelled         = False
        self.replace            = replace
        # Rebuild the municipality/locality index after the import (2–3 minutes
        # for large DBs). When importing several files in a row (automatic
        # source download), enable it only for the last file.
        self.rebuild_lookup     = rebuild_lookup
        # announce=False: called from the automatic download — file name and
        # Bundesland are known or irrelevant there (detail channel only).
        self.announce           = announce
        self._plz_source        = None
        self._plz_field         = 'plz'
        self._plz_layername     = None

    def set_plz_layer(self, source, field='plz', layername=None):
        """Optional PLZ area layer; missing PLZ are filled in during import."""
        self._plz_source    = source
        self._plz_field     = field or 'plz'
        self._plz_layername = layername

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            import traceback
            self.detail.emit(traceback.format_exc())
            self.finished.emit(False, f'Import fehlgeschlagen: {e}. '
                                      'Der bisherige Datenbestand bleibt erhalten.')

    def _run(self):
        (self.log if self.announce else self.detail).emit(
            f'Lese {os.path.basename(self.filepath)} …')
        self.detail.emit(f'Datei: {self.filepath}')

        # Detect the format
        fmt = _detect_format(self.filepath)
        if fmt == 'saarland':
            col = _COL_SAARLAND
            fmt_label = 'Saarland (eigenes 20-spaltiges Format)'
        elif fmt == 'berlin':
            col = _COL_BERLIN
            fmt_label = 'Berlin (HK-DE 25-spaltig)'
        elif fmt == 'sachsen_anhalt':
            col = _COL_SACHSEN_ANHALT
            fmt_label = 'Sachsen-Anhalt (HK-DE 26-spaltig, ohne PLZ)'
        elif fmt == 'baden_wuerttemberg':
            col = _COL_BW
            fmt_label = 'Baden-Württemberg (HK-DE 26-spaltig, ohne PLZ)'
        elif fmt == 'brandenburg':
            col = _COL_BRANDENBURG
            fmt_label = 'Brandenburg (HK-DE 26-spaltig, mit PLZ)'
        elif fmt == 'sachsen':
            col = _COL_SACHSEN
            fmt_label = 'HK-DE 24-spaltig (Sachsen/Thüringen/MV/Niedersachsen u.a.)'
        elif fmt == 'bayern':
            col = _COL_BAYERN
            fmt_label = 'Bayern (HK-BY 24-spaltig, Quelle: OpenStreetMap/ODbL)'
        else:
            col = _COL_NRW
            fmt_label = 'HK-DE-Standard (20-spaltig)'
        self.detail.emit(f'Format erkannt: {fmt_label}')

        # Estimate the line count (for the progress bar)
        total = self._count_lines()
        self.detail.emit(f'Geschätzte Zeilenzahl: {_n(total)}')

        conn = sqlite3.connect(_lc.DB_PATH)
        init_db(conn)

        # Determine the Bundesland from the first data line if not given
        bundesland = self.bundesland_override
        if not bundesland:
            bundesland = self._detect_bundesland_with_col(col)
        (self.log if self.announce else self.detail).emit(f'Bundesland: {bundesland}')

        if self.replace:
            # IMPORTANT: do NOT commit the DELETE right away! Deletion and insert
            # run in ONE transaction (commit only at the very end). If the import
            # fails, a rollback undoes everything and the existing data is
            # kept — instead of being deleted and empty.
            self.log.emit(f'Die bisherigen Adressen für {bundesland} werden ersetzt.')
            conn.execute("DELETE FROM addresses WHERE bundesland = ?", (bundesland,))

        encoding = self._detect_encoding()
        self.detail.emit(f'Encoding: {encoding}')

        delim = _detect_delimiter(self.filepath)
        if delim == ',':
            self.detail.emit('Trennzeichen: Komma (CSV mit Anführungszeichen).')

        batch       = []
        n_ok        = 0
        n_skip      = 0
        n_lines     = 0

        # PLZ fill-in: without a custom layer, the bundled postcode areas
        resolver = build_resolver_logged(self._plz_source, self._plz_field,
                                         self._plz_layername, self.log.emit,
                                         self.detail.emit)

        try:
            with open(self.filepath, encoding=encoding, errors='replace',
                      newline='') as f:
                for parts in _iter_parts(f, delim):
                    if self._cancelled:
                        conn.rollback()   # discard the deletion + inserts so far
                        conn.close()
                        self.finished.emit(False, 'Import abgebrochen – der bisherige '
                                                  'Datenbestand bleibt erhalten.')
                        return

                    n_lines += 1
                    if n_lines % 20000 == 0:
                        self.progress.emit(n_lines, total, f'{_n(n_ok)} Adressen eingelesen …')

                    if not parts:
                        continue

                    if parts[0].strip().lower() in ('nba', 'kennung'):
                        continue   # skip the header row (HK-BY and others)
                    if fmt in ('sachsen', 'brandenburg', 'bayern'):
                        min_cols = 21
                    else:
                        min_cols = 20
                    if len(parts) < min_cols:
                        n_skip += 1
                        continue

                    try:
                        if fmt == 'saarland':
                            # Saarland: comma decimal, zone as prefix in XCOORD
                            zone_sl, x, y = _parse_saarland_xy(
                                parts[col['x']], parts[col['y']])
                        else:
                            x = float(parts[col['x']].replace(',', '.'))
                            y = float(parts[col['y']].replace(',', '.'))
                    except (ValueError, IndexError):
                        n_skip += 1
                        continue

                    strasse = parts[col['strasse']].strip()
                    if not strasse:
                        n_skip += 1
                        continue

                    hnr_raw = parts[col['hausnummer']].strip()
                    if hnr_raw and not _is_plausible_hausnummer(hnr_raw):
                        # Implausible pseudo house number (e.g. parcel/forest ID
                        # like '84068997' in Baden-Württemberg) -> skip
                        n_skip += 1
                        continue

                    # bundesland_name=-3 = Saarland special case: the LAN column is
                    # only a numeric key ('10'), not a plain-text name
                    if col['bundesland_name'] == -3:
                        bl = bundesland or 'Saarland'
                    else:
                        bl = bundesland or _canonical_bundesland(parts[col['bundesland_name']].strip())

                    # gemeinde=-2 = Berlin special case: fixed city name instead of a column
                    if col['gemeinde'] == -2:
                        gemeinde_val = bl  # e.g. 'Berlin'
                    else:
                        gemeinde_val = _clean_gemeinde(parts[col['gemeinde']].strip())
                        # HK-BY: gmd may be empty -> use postonm as fallback
                        if not gemeinde_val:
                            fb = col.get('gemeinde_fallback', -1)
                            if 0 <= fb < len(parts):
                                gemeinde_val = _clean_gemeinde(parts[fb].strip())
                    plz_val      = (parts[col['plz']].strip()
                                    if col['plz'] >= 0 and col['plz'] < len(parts)
                                    else '')
                    if col['epsg'] == -4:
                        epsg_val = _normalize_epsg(zone_sl)
                    else:
                        epsg_val = _normalize_epsg(parts[col['epsg']].strip())

                    herkunft = None
                    if not plz_val and resolver is not None:
                        plz_val = resolver.lookup(x, y, epsg_val)
                        herkunft = 'gebiet' if plz_val else None

                    batch.append((
                        bl,
                        parts[col['kreis']].strip(),
                        gemeinde_val,
                        plz_val,
                        strasse,
                        parts[col['hausnummer']].strip(),
                        parts[col['hausnr_zusatz']].strip(),
                        epsg_val,
                        x,
                        y,
                        normalize(strasse),
                        _core_gemeinde(gemeinde_val),
                        herkunft,
                    ))
                    n_ok += 1

                    if len(batch) >= BATCH_SIZE:
                        self._flush(conn, batch)
                        batch = []

            if batch:
                self._flush(conn, batch)

            if n_ok == 0:
                # Empty or wrong file (e.g. maintenance page instead of data): replace
                # nothing — otherwise the state's existing data would be deleted
                conn.rollback()
                conn.close()
                self.finished.emit(False, 'In der Datei wurden keine Adressen gefunden – der bisherige '
                                          'Datenbestand bleibt erhalten.')
                return
            if resolver is not None:
                st = resolver.stats()
                if st["matched"] or st["missed"]:   # nothing to fill in → no message
                    self.log.emit(f"PLZ ergänzt für {_n(st['matched'])} Adressen"
                                  + (f"; {_n(st['missed'])} liegen außerhalb der PLZ-Flächen."
                                     if st['missed'] else '.'))
            _lc.record_plz_meta(conn, bundesland,
                                _lc.resolver_info(resolver, self._plz_source,
                                                  self._plz_field, self._plz_layername),
                                self.replace)

            # Municipality/locality index for the new data (part of the same
            # transaction), then ONE atomic commit including the DELETE.
            if self.rebuild_lookup:
                _lc.rebuild_lookup_tables(conn, self.log.emit)
            conn.commit()
        except Exception:
            conn.rollback()     # discard DELETE + inserts so far completely
            conn.close()
            raise               # run() reports the error; existing data is kept
        conn.close()

        self.progress.emit(n_lines, total, 'Fertig.')
        msg = f'Import abgeschlossen: {_n(n_ok)} Adressen importiert'
        if n_skip:
            msg += f' ({_n(n_skip)} Zeilen ohne verwertbare Adresse übersprungen)'
        self.finished.emit(True, msg + '.')

    def _flush(self, conn, batch):
        # NO commit per batch — the whole import (including the preceding DELETE)
        # is ONE transaction and is committed atomically only at the end in _run.
        conn.executemany("""
            INSERT INTO addresses
              (bundesland, kreis, gemeinde, plz, strasse, hausnummer,
               hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm, plz_herkunft)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, batch)

    def _count_lines(self):
        try:
            with open(self.filepath, 'rb') as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _detect_encoding(self):
        for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
            try:
                with open(self.filepath, encoding=enc) as f:
                    f.read(8192)
                return enc
            except UnicodeDecodeError:
                continue
        return 'latin-1'

    def _detect_bundesland(self):
        _fmt = _detect_format(self.filepath)
        if _fmt == 'saarland':
            col = _COL_SAARLAND
        elif _fmt == 'berlin':
            col = _COL_BERLIN
        elif _fmt == 'sachsen_anhalt':
            col = _COL_SACHSEN_ANHALT
        elif _fmt == 'baden_wuerttemberg':
            col = _COL_BW
        elif _fmt == 'brandenburg':
            col = _COL_BRANDENBURG
        elif _fmt == 'sachsen':
            col = _COL_SACHSEN
        else:
            col = _COL_NRW
        return self._detect_bundesland_with_col(col)

    def _detect_bundesland_with_col(self, col):
        if col['bundesland_name'] == -3:
            return 'Saarland'
        enc = self._detect_encoding()
        delim = _detect_delimiter(self.filepath)
        try:
            with open(self.filepath, encoding=enc, errors='replace', newline='') as f:
                for parts in _iter_parts(f, delim):
                    idx = col['bundesland_name']
                    if len(parts) > idx:
                        bl = parts[idx].strip()
                        if bl and bl.lower() not in ('land', 'bundesland', 'bl', ''):
                            return _canonical_bundesland(bl)
        except Exception:
            pass
        return 'Unbekannt'

    @staticmethod
    def preview_file(filepath, n_lines=3):
        """Returns the first n data lines as a list of dicts.
        Format-aware: picks the matching column map (Standard/Sachsen/Berlin)
        based on the detected column count and returns canonical Bundesland names.
        """
        fmt = _detect_format(filepath)
        if fmt == 'saarland':
            col = _COL_SAARLAND
        elif fmt == 'berlin':
            col = _COL_BERLIN
        elif fmt == 'sachsen_anhalt':
            col = _COL_SACHSEN_ANHALT
        elif fmt == 'baden_wuerttemberg':
            col = _COL_BW
        elif fmt == 'brandenburg':
            col = _COL_BRANDENBURG
        elif fmt == 'sachsen':
            col = _COL_SACHSEN
        elif fmt == 'bayern':
            col = _COL_BAYERN
        else:
            col = _COL_NRW

        results = []
        delim = _detect_delimiter(filepath)
        for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
            try:
                with open(filepath, encoding=enc, errors='replace', newline='') as f:
                    for parts in _iter_parts(f, delim):
                        min_cols = 21 if fmt in ('sachsen', 'brandenburg', 'bayern') else 20
                        if len(parts) < min_cols:
                            continue
                        if col['bundesland_name'] == -3:
                            bl = 'Saarland'
                        else:
                            bl = parts[col['bundesland_name']].strip()
                        # skip the header row: contains column names instead of data
                        if bl.lower() in ('land', 'bundesland', 'bl', 'state', ''):
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
                break
            except UnicodeDecodeError:
                continue
        return results
