# -*- coding: utf-8 -*-

import time
import urllib.request
import urllib.parse
import json
import re

from qgis.PyQt.QtCore import QObject, pyqtSignal

import os

from .local_geocoder import (search as local_search, db_exists, normalize,
                             get_db_stats, diagnose_search, locate_locality,
                             street_representative, bundesland_from_plz,
                             canonical_bundesland, GENERIC_COUNTRY_NAMES,
                             ensure_lookup_tables, normalize_hausnummer)


def _plugin_version():
    """Version from metadata.txt (for the Nominatim User-Agent)."""
    try:
        meta = os.path.join(os.path.dirname(__file__), 'metadata.txt')
        with open(meta, encoding='utf-8') as f:
            for line in f:
                if line.strip().lower().startswith('version='):
                    return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return 'unbekannt'


PLUGIN_VERSION = _plugin_version()


ACCURACY_MAP = {
    'building':      (5, 'Gebäude'),
    'house':         (5, 'Hausnummer'),
    'road':          (4, 'Straße'),
    'residential':   (4, 'Straße'),
    'pedestrian':    (4, 'Straße'),
    'footway':       (4, 'Straße'),
    'cycleway':      (4, 'Straße'),
    'path':          (4, 'Straße'),
    'square':        (4, 'Platz'),
    'postcode':      (3, 'Postleitzahl'),
    'suburb':        (3, 'Stadtteil'),
    'neighbourhood': (3, 'Stadtteil'),
    'quarter':       (3, 'Stadtteil'),
    'city':          (2, 'Stadt'),
    'town':          (2, 'Stadt'),
    'village':       (2, 'Ort'),
    'hamlet':        (2, 'Ort'),
    'municipality':  (2, 'Gemeinde'),
    'county':        (1, 'Landkreis'),
    'state_district':(1, 'Bezirk'),
    'state':         (1, 'Bundesland'),
    'country':       (0, 'Land'),
}

CATEGORY_TYPE_MAP = {
    ('building',  'yes'):           (5, 'Gebäude'),
    ('building',  'house'):         (5, 'Gebäude'),
    ('building',  'apartments'):    (5, 'Gebäude'),
    ('building',  'commercial'):    (5, 'Gebäude'),
    ('building',  'industrial'):    (5, 'Gebäude'),
    ('building',  'office'):        (5, 'Gebäude'),
    ('amenity',   'fire_station'):  (5, 'Einrichtung (Adresse)'),
    ('amenity',   'school'):        (5, 'Einrichtung (Adresse)'),
    ('amenity',   'hospital'):      (5, 'Einrichtung (Adresse)'),
    ('amenity',   'police'):        (5, 'Einrichtung (Adresse)'),
    ('amenity',   'townhall'):      (5, 'Einrichtung (Adresse)'),
    ('amenity',   'post_office'):   (5, 'Einrichtung (Adresse)'),
    ('amenity',   'pharmacy'):      (5, 'Einrichtung (Adresse)'),
    ('amenity',   'bank'):          (5, 'Einrichtung (Adresse)'),
    ('amenity',   'fuel'):          (5, 'Einrichtung (Adresse)'),
    ('shop',      None):            (5, 'Einrichtung (Adresse)'),
    ('office',    None):            (5, 'Einrichtung (Adresse)'),
    ('leisure',   None):            (5, 'Einrichtung (Adresse)'),
    ('tourism',   None):            (5, 'Einrichtung (Adresse)'),
    ('place',     'house'):         (5, 'Hausnummer'),
    ('place',     'building'):      (5, 'Gebäude'),
    ('place',     'square'):        (4, 'Platz'),
    ('highway',   'pedestrian'):    (4, 'Platz'),
    ('leisure',   'park'):          (4, 'Platz/Park'),
    ('place',     'farm'):          (5, 'Einrichtung (Adresse)'),
    ('place',     'hamlet'):        (2, 'Ort'),
    ('place',     'isolated_dwelling'): (3, 'Stadtteil'),
    ('highway',   'residential'):   (4, 'Straße'),
    ('highway',   'primary'):       (4, 'Straße'),
    ('highway',   'secondary'):     (4, 'Straße'),
    ('highway',   'tertiary'):      (4, 'Straße'),
    ('highway',   'unclassified'):  (4, 'Straße'),
    ('highway',   'living_street'): (4, 'Straße'),
    ('highway',   'road'):          (4, 'Straße'),
}


def calc_accuracy_nominatim(addresstype, type_, category, importance):
    if addresstype in ACCURACY_MAP:
        level, label = ACCURACY_MAP[addresstype]
    elif (category, type_) in CATEGORY_TYPE_MAP:
        level, label = CATEGORY_TYPE_MAP[(category, type_)]
    elif (category, None) in CATEGORY_TYPE_MAP:
        level, label = CATEGORY_TYPE_MAP[(category, None)]
    else:
        level, label = 0, 'Unbekannt'
    if importance is not None and float(importance) > 0.5 and level > 0:
        level = min(level + 1, 5)
    return level, label


# The PLZ-prefix→Bundesland table lives centrally in local_geocoder
# (PLZ_PREFIX_TO_BUNDESLAND / bundesland_from_plz) and is also used by the
# dialog (admin preview) — one source instead of two diverging ones.


def _detect_bundesland(parts, db_bundeslaender):
    """Detect from the address fields which Bundesland (federal state) is meant.

    Order:
    1. land field set → direct match against the Bundesländer in the DB
    2. PLZ prefix → Bundesland mapping
    3. No hint → None (search without Bundesland filter)

    Returns (bundesland_str_or_None, should_try_bool).
    """
    # 1. Explicit land (country/state) field
    # Ignore generic country names (Deutschland, Germany etc.) and fall back
    # to the PLZ prefix instead — otherwise "Deutschland" would block the
    # local search because it is not a Bundesland name. Abbreviations (NRW,
    # BW …), missing hyphens and official suffixes are resolved via the alias
    # table — previously e.g. "NRW" switched off the local search entirely.
    land = normalize(parts.get('land', ''))
    if land and land not in GENERIC_COUNTRY_NAMES:
        bl = canonical_bundesland(land)
        if bl and bl in db_bundeslaender:
            return bl, True
        if bl:
            # Recognized Bundesland, but not imported → local search is pointless.
            return None, False
        # Unknown value (typo, foreign country, Landkreis in the land
        # column): do not block, continue searching via PLZ/Gemeinde.

    # 2. PLZ prefix
    bl = bundesland_from_plz(parts.get('plz', ''))
    if bl and bl in db_bundeslaender:
        return bl, True

    # 3. No hint → still try locally (no Bundesland filter)
    if db_bundeslaender:
        return None, True   # None = no filter, search all Bundesländer
    return None, False


_PLZ_LINE_RE = re.compile(r'\b(\d{5})\b')


def _parse_address_line(text):
    """Split a single-line address into strasse/plz/stadt (land stays empty).

    Heuristic:
      - PLZ = first 5-digit number in the text.
      - With commas: the segment containing the PLZ becomes the place (PLZ
        removed); the segments before it become the street. Without a PLZ:
        first segment = street, last segment = place.
      - Without commas: text before the PLZ = street, text after it = place.
        Without any PLZ everything goes into the street (the Nominatim
        fallback uses the full text).
    """
    out = {'strasse': '', 'hausnummer': '', 'plz': '', 'stadt': '', 'land': ''}
    text = (text or '').strip()
    if not text:
        return out

    m   = _PLZ_LINE_RE.search(text)
    plz = m.group(1) if m else ''
    out['plz'] = plz

    if ',' in text:
        segs = [s.strip() for s in text.split(',') if s.strip()]
        plz_idx = None
        for i, seg in enumerate(segs):
            if plz and plz in seg:
                plz_idx = i
                break
        if plz_idx is not None:
            ort = segs[plz_idx].replace(plz, '').strip(' ,')
            if not ort and plz_idx + 1 < len(segs):
                ort = segs[plz_idx + 1]
            out['stadt']   = ort
            street         = ', '.join(segs[:plz_idx]).strip()
            if not street and plz_idx + 1 < len(segs):
                # Place/PLZ come first → street follows after the PLZ segment
                street = ', '.join(segs[plz_idx + 1:]).strip()
            out['strasse'] = street or (segs[0] if segs else '')
        else:
            out['strasse'] = segs[0]
            if len(segs) > 1:
                out['stadt'] = segs[-1]
    else:
        if plz:
            out['strasse'] = text[:m.start()].strip(' ,')
            out['stadt']   = text[m.end():].strip(' ,')
        else:
            out['strasse'] = text
    return out


def _split_hausnummer(strasse):
    """Split the house number off the street text (same rule as the local
    search): 'Lindenstraße 12 a' -> ('lindenstraße', '12 a')."""
    from .local_geocoder import _extract_hausnummer
    return _extract_hausnummer(normalize(strasse))


class GeocoderWorker(QObject):

    progress     = pyqtSignal(int, int, str)
    result_ready = pyqtSignal(list)
    finished     = pyqtSignal()

    def __init__(self, rows, col_map, delay=1.1, local_only=False,
                 bundeslaender=None):
        super().__init__()
        self.rows              = rows
        self.col_map           = col_map
        self.delay             = delay
        self.local_only        = local_only
        self._cancelled        = False
        self._has_local        = db_exists()
        # List of imported Bundesländer. The dialog passes its cached
        # statistics; if missing, they are determined only in run() — i.e. in
        # the worker thread. The constructor runs in the UI thread, where a
        # GROUP BY over millions of rows would freeze QGIS for seconds.
        self._db_bundeslaender = list(bundeslaender) if bundeslaender else None
        self._local_diagnose   = ''

    def cancel(self):
        self._cancelled = True

    def run(self):
        results        = []
        total          = len(self.rows)
        last_nominatim = 0.0

        # Ensure the search indexes once (idempotent; returns immediately if
        # the indexes exist). Keeps the local search fast on databases
        # imported by older versions, too.
        if self._has_local:
            try:
                from .local_geocoder import ensure_search_indexes
                ensure_search_indexes()
            except Exception:
                pass
            if self._db_bundeslaender is None:
                try:
                    self._db_bundeslaender = list(get_db_stats().keys())
                except Exception:
                    self._db_bundeslaender = []
            # Gemeinde/Ortsteil directory (one-time, 2–3 minutes on the nationwide DB);
            # reported via the progress text so it does not look like a hang.
            try:
                ensure_lookup_tables(log=lambda m: self.progress.emit(0, total, m))
            except Exception:
                pass
        elif self._db_bundeslaender is None:
            self._db_bundeslaender = []

        for i, row in enumerate(self.rows):
            if self._cancelled:
                break

            address_parts = self._build_parts(row)
            address_str   = self._format_address(address_parts)
            self.progress.emit(i + 1, total, address_str)

            result = None

            if self._has_local:
                bundesland, should_try = _detect_bundesland(
                    address_parts, self._db_bundeslaender)

                # Without a place anchor (neither PLZ nor place) the local
                # search would be a nationwide street search: "Hauptstraße 5"
                # would then find some arbitrary Hauptstraße 5 and report it
                # as a confident hit. Such rows go straight to Nominatim, or
                # stay unresolved in DSGVO (GDPR) mode.
                if should_try and not (address_parts.get('plz', '').strip()
                                       or address_parts.get('stadt', '').strip()):
                    should_try = False
                    self._local_diagnose = ('Keine PLZ und kein Ort angegeben – '
                                            'lokale Suche nicht möglich')

                if should_try:
                    result = self._geocode_local(address_parts, bundesland)
                    if result['geocoded']:
                        bl_label = result.get('matched_bundesland') or bundesland or 'DB'
                        result['source'] = f'Lokal ({bl_label})'
                    else:
                        # Local search failed — determine the reason (for the
                        # 'local_diagnose' column; explains why the fallback ran)
                        try:
                            self._local_diagnose = diagnose_search(
                                strasse    = ' '.join(x for x in (
                                    address_parts.get('strasse', ''),
                                    address_parts.get('hausnummer', '')) if x),
                                plz        = address_parts.get('plz', ''),
                                gemeinde   = address_parts.get('stadt', ''),
                                bundesland = bundesland)
                        except Exception:
                            self._local_diagnose = ''
                    self._last_bundesland = bundesland

            # No local hit or local DB not responsible → Nominatim, except in
            # DSGVO (GDPR) mode (local sources only, no online query).
            if result is None or not result['geocoded']:
                if self.local_only:
                    if result is None:
                        result = self._empty_result()
                    result['accuracy_label'] = 'Nicht gefunden (nur lokal)'
                    result['source']         = 'Lokal (kein Treffer)'
                else:
                    elapsed = time.time() - last_nominatim
                    if elapsed < self.delay:
                        time.sleep(self.delay - elapsed)
                    nom_result     = self._geocode_nominatim(address_str)
                    last_nominatim = time.time()
                    if result and not result['geocoded']:
                        nom_result['source'] = 'Nominatim (Fallback)'
                    else:
                        nom_result['source'] = 'Nominatim'
                    result = nom_result

            result['original_row']  = row
            result['address_query'] = address_str
            # Attach the diagnostic note whenever the local search failed —
            # regardless of whether Nominatim was tried afterwards (online mode)
            # or not (DSGVO mode). _local_diagnose is empty on a local hit, so
            # it correctly stays '' there.
            result['local_diagnose'] = getattr(self, '_local_diagnose', '')
            self._local_diagnose = ''
            results.append(result)

        self.result_ready.emit(results)
        self.finished.emit()

    def _build_parts(self, row):
        cm = self.col_map
        # Single-column mode: the complete address is in one column → split it
        if 'combined' in cm:
            col = cm.get('combined', '')
            raw = row[col].strip() if col and col in row else ''
            return _parse_address_line(raw)
        parts = {}
        for field in ['strasse', 'hausnummer', 'plz', 'stadt', 'land']:
            col          = cm.get(field, '')
            parts[field] = row[col].strip() if col and col in row else ''
        return parts

    @staticmethod
    def _format_address(parts):
        """'Straße Hausnummer, PLZ, Ort, Land' — a separately mapped house
        number belongs after the street, not as its own comma-separated part."""
        strasse = ' '.join(x for x in (parts.get('strasse', ''),
                                       parts.get('hausnummer', '')) if x)
        return ', '.join(x for x in (strasse, parts.get('plz', ''),
                                     parts.get('stadt', ''), parts.get('land', '')) if x)

    # ── Local search ─────────────────────────────────────────────────────────

    def _geocode_local(self, parts, bundesland=None):
        hits = local_search(
            strasse    = parts.get('strasse', ''),
            hausnummer = parts.get('hausnummer', ''),
            plz        = parts.get('plz', ''),
            gemeinde   = parts.get('stadt', ''),
            bundesland = bundesland,
            max_results= 1,
        )
        if hits:
            h = hits[0]
            # House-number level (5) only if the user gave a house number AND it
            # was matched exactly. Otherwise street level (4) — and NO house
            # number taken from the DB row ("invented") in the name.
            exact_hn = bool(h.get('hn_requested')) and float(h.get('hn_score') or 0) >= 1.0
            # Append the note from the search (e.g. 'Ort mehrdeutig (3 Orte),
            # größter gewählt') to the score label.
            score_label = h['score_label']
            if h.get('hinweis'):
                score_label = f"{score_label} — {h['hinweis']}"
            if exact_hn:
                return {
                    'geocoded':          True,
                    'name':              f"{h['strasse']} {h['hausnummer']}".strip(),
                    'addresstype':       'house',
                    'importance':        None,
                    'type':              'house',
                    'category':          'building',
                    'accuracy_level':    5,
                    'accuracy_label':    'Hausnummer',
                    'local_score':       h['score'],
                    'local_score_label': score_label,
                    'lat':               None,
                    'lon':               None,
                    'utm_x':             h['x'],
                    'utm_y':             h['y'],
                    'epsg':              h['epsg'],
                    'matched_bundesland': h['bundesland'],
                }
            # Street level: do NOT take the coordinate of an arbitrary house
            # number on the street (that is how "Lorettoplatz 999" used to end
            # up at Lorettoplatz 7), but a well-defined point: the numerically
            # nearest house number or — without a house number in the input —
            # the address point at the street midpoint.
            x, y, epsg = h['x'], h['y'], h['epsg']
            label, name = 'Straße', h['strasse']
            try:
                hn_in = normalize_hausnummer(parts.get('hausnummer', ''))
                if not hn_in:
                    _, hn_in = _split_hausnummer(parts.get('strasse', ''))
                rp = street_representative(
                    h.get('strasse_norm'), h.get('gemeinde_norm'),
                    bundesland=h['bundesland'],
                    plz=h.get('plz') or parts.get('plz', ''),
                    hausnummer=hn_in if h.get('hn_requested') else '')
            except Exception:
                rp = None
            if rp:
                x, y, epsg = rp['x'], rp['y'], rp['epsg']
                if rp['kind'] == 'nearest':
                    label = 'Straße (nächste Hausnummer)'
                    name  = f"{h['strasse']} {rp['hausnummer']}".strip()
                else:
                    label = 'Straße (Mittelpunkt)'
            return {
                'geocoded':          True,
                'name':              name,
                'addresstype':       'road',
                'importance':        None,
                'type':              'road',
                'category':          'highway',
                'accuracy_level':    4,
                'accuracy_label':    label,
                'local_score':       h['score'],
                'local_score_label': score_label,
                'lat':               None,
                'lon':               None,
                'utm_x':             x,
                'utm_y':             y,
                'epsg':              epsg,
                'matched_bundesland': h['bundesland'],
            }
        # No (matching) street hit, but PLZ or place present → use a coarser
        # centroid instead of a NULL geometry (accuracy_level=2).
        loc = locate_locality(
            plz        = parts.get('plz', ''),
            gemeinde   = parts.get('stadt', ''),
            bundesland = bundesland,
        )
        if loc:
            is_plz = loc['label'].startswith('PLZ')
            return {
                'geocoded':          True,
                'name':              (parts.get('stadt', '') or parts.get('plz', '')),
                'addresstype':       'postcode' if is_plz else 'city',
                'importance':        None,
                'type':              'postcode' if is_plz else 'administrative',
                'category':          'place',
                'accuracy_level':    loc['level'],
                'accuracy_label':    loc['label'],
                'local_score':       None,
                'local_score_label': 'Näherung (Schwerpunkt)',
                'lat':               None,
                'lon':               None,
                'utm_x':             loc['x'],
                'utm_y':             loc['y'],
                'epsg':              loc['epsg'],
                'matched_bundesland': loc['bundesland'],
            }
        return self._empty_result()

    # ── Nominatim ────────────────────────────────────────────────────────────

    def _geocode_nominatim(self, address):
        try:
            params = urllib.parse.urlencode({
                'q':              address,
                'format':         'jsonv2',
                'limit':          1,
                'addressdetails': 1,
            })
            url = f'https://nominatim.openstreetmap.org/search?{params}'
            req = urllib.request.Request(url, headers={
                'User-Agent': f'QGIS-GISAkademieGeocoderPlugin/{PLUGIN_VERSION} '
                              '(wieschmann@gis-akademie.de)'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data:
                    geo = data[0]
                    imp = geo.get('importance')
                    at  = geo.get('addresstype', '')
                    t   = geo.get('type', '')
                    cat = geo.get('category', '')
                    lvl, lbl = calc_accuracy_nominatim(at, t, cat, imp)
                    return {
                        'geocoded':          True,
                        'name':              geo.get('name', ''),
                        'addresstype':       at,
                        'importance':        round(float(imp), 4) if imp else None,
                        'type':              t,
                        'category':          cat,
                        'accuracy_level':    lvl,
                        'accuracy_label':    lbl,
                        'local_score':       None,
                        'local_score_label': '',
                        'lat':               float(geo.get('lat', 0)),
                        'lon':               float(geo.get('lon', 0)),
                        'utm_x':             None,
                        'utm_y':             None,
                        'epsg':              4326,
                    }
        except Exception as e:
            # A network/server error is different from "no hit" — mark it in
            # the result so the user can tell them apart.
            res = self._empty_result()
            code = getattr(e, 'code', None)
            if code == 429:
                res['accuracy_label'] = 'Nominatim: zu viele Anfragen – später erneut versuchen'
            elif code:
                res['accuracy_label'] = f'Nominatim antwortet mit Fehler {code}'
            else:
                res['accuracy_label'] = 'Nominatim nicht erreichbar'
            return res
        return self._empty_result()

    def _empty_result(self):
        return {
            'geocoded':          False,
            'name':              '',
            'addresstype':       '',
            'importance':        None,
            'type':              '',
            'category':          '',
            'accuracy_level':    0,
            'accuracy_label':    'Nicht gefunden',
            'local_score':       None,
            'local_score_label': '',
            'lat':               None,
            'lon':               None,
            'utm_x':             None,
            'utm_y':             None,
            'epsg':              None,
        }
