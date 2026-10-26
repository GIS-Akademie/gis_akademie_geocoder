# -*- coding: utf-8 -*-
"""
Import worker for the INSPIRE GML format (Hamburg and other Bundesländer).

Structure:
  - ad:ThoroughfareName  → street name (attribute: text)
  - ad:PostalDescriptor  → postcode/PLZ (ad:postCode; fallback: 5-digit suffix
                           of the gml:id, PostalDescriptor_22359 → 22359)
  - ad:AdminUnitName     → administrative unit (most specific = district/municipality)
  - ad:Address           → house number, coordinates, references to the above

Strategy: two-pass streaming with xml.etree.ElementTree iterparse.
  Pass 1: load all ThoroughfareName, AdminUnitName and PostalDescriptor blocks into dicts
  Pass 2: process Address features and resolve references
"""

import os
import re
import xml.etree.ElementTree as ET

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .db_import_worker import (
    AddressSink, CANCEL_MSG, KEEP_MSG, _clean_gemeinde, _as_list,
)
from .plz_resolver import build_resolver_logged


def _n(x):
    """Integer with German thousands separator (dot): 4509537 -> '4.509.537'."""
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


# XML namespaces
NS = {
    'gml': 'http://www.opengis.net/gml/3.2',
    'ad':  'http://inspire.ec.europa.eu/schemas/ad/4.0',
    'gn':  'http://inspire.ec.europa.eu/schemas/gn/4.0',
    'xlink': 'http://www.w3.org/1999/xlink',
    'wfs': 'http://www.opengis.net/wfs/2.0',
    'base': 'http://inspire.ec.europa.eu/schemas/base/3.3',
}


def _tag(ns, local):
    return f'{{{NS[ns]}}}{local}'


# Feature containers: gml:featureMember (file download, e.g. Hamburg),
# wfs:member (WFS 2.0 GetFeature response, e.g. Mecklenburg-Vorpommern) or
# base:member (INSPIRE SpatialDataSet, e.g. Thüringen).
_MEMBER_TAGS = {_tag('gml', 'featureMember'), _tag('wfs', 'member'), _tag('base', 'member')}
_XLINK_HREF = f'{{{NS["xlink"]}}}href'
_GML_ID = f'{{{NS["gml"]}}}id'


def ref_id(href):
    """Identifier from an xlink reference: '#ThoroughfareName_1' or
    '…/de.bb.inspire.ad.alkis/ThoroughfareName_1' (Brandenburg: full URL
    without '#') → 'ThoroughfareName_1'."""
    href = href or ''
    if '#' in href:
        return href.rsplit('#', 1)[-1]
    return href.rstrip('/').rsplit('/', 1)[-1]


def _is_member(tag):
    return tag in _MEMBER_TAGS


def _level_of(au_elem):
    """INSPIRE administrative level of an AdminUnitName ('1stOrder' … '6thOrder')
    from ad:level (xlink:href or text), otherwise ''."""
    lvl = au_elem.find(_tag('ad', 'level'))
    if lvl is None:
        return ''
    href = lvl.get(_XLINK_HREF, '') or ''
    val  = href.rstrip('/').rsplit('/', 1)[-1] if href else (lvl.text or '')
    return val.strip()


def _designator_kind(href):
    """Code of a LocatorDesignatorTypeValue reference: the last path segment,
    e.g. '…/LocatorDesignatorTypeValue/addressNumber' (with or without a
    trailing slash) → 'addressNumber', '…/addressNumberExtension' →
    'addressNumberExtension'."""
    return ref_id(href or '')


_PLZ_RE = re.compile(r'^\d{5}$')


def _plz_of(components, postal_codes):
    """5-digit PLZ of an address: ad:postCode of the referenced
    PostalDescriptor, otherwise the id suffix (PostalDescriptor_22359) if it is
    exactly 5 digits — else '' (then the PLZ fill-in from the areas applies)."""
    for comp in components:
        code = postal_codes.get(comp)
        if code and _PLZ_RE.match(code):
            return code
        if comp.startswith('PostalDescriptor_'):
            suffix = comp[len('PostalDescriptor_'):]
            if _PLZ_RE.match(suffix):
                return suffix
    return ''


# ── Coordinate reference system from srsName ────────────────────────────────

_SRS_PATTERNS = (
    # urn:ogc:def:crs:EPSG::25833 and versioned urn:ogc:def:crs:EPSG:6.12:25833
    (re.compile(r'urn:ogc:def:crs:EPSG:[^:]*:(\d+)\s*$', re.I), True),
    # http(s)://www.opengis.net/def/crs/EPSG/0/25833
    (re.compile(r'/def/crs/EPSG/[^/]*/(\d+)\s*$', re.I), True),
    # http://www.opengis.net/gml/srs/epsg.xml#25833 (traditional axis order)
    (re.compile(r'epsg\.xml#(\d+)\s*$', re.I), False),
    # EPSG:25833 (traditional axis order)
    (re.compile(r'^\s*EPSG:(\d+)\s*$', re.I), False),
)
_ADV_RE = re.compile(r'urn:adv:crs:ETRS89_UTM(\d{2})', re.I)
_LATLON_EPSG = (4258, 4326)       # geographic CRS with lat/lon axis order (URN/URI form)
_SRS_CACHE = {}


def parse_srs_name(srs, default=25832):
    """srsName -> (epsg, swap_axes). swap_axes is True for 4258/4326 in URN/URI
    form, whose axis order is latitude/longitude (x must become the longitude).
    Unknown or missing: (default, False)."""
    srs = (srs or '').strip()
    hit = _SRS_CACHE.get(srs)
    if hit is not None:
        return hit
    res = None
    for rx, latlon_order in _SRS_PATTERNS:
        m = rx.search(srs)
        if m:
            epsg = int(m.group(1))
            res = (epsg, latlon_order and epsg in _LATLON_EPSG)
            break
    if res is None:
        m = _ADV_RE.search(srs)
        if m:
            res = (25800 + int(m.group(1)), False)
    if res is None:
        m = re.search(r'EPSG:{1,2}(\d+)', srs, re.I)
        res = (int(m.group(1)), False) if m else (default, False)
    _SRS_CACHE[srs] = res
    return res


class _GmlBroken(Exception):
    """XML error in an input file (message for the user)."""


class GmlImportWorker(QObject):

    progress  = pyqtSignal(int, int, str)
    log       = pyqtSignal(str)             # log for the user
    detail    = pyqtSignal(str)             # technical details (QGIS message log)
    finished  = pyqtSignal(bool, str)

    def __init__(self, filepaths, bundesland, replace=True, gemeinde_level=None,
                 rebuild_lookup=True, announce=True, allow_shrink=False):
        """filepaths: one GML file or a LIST of files (e.g. the pages of a
        WFS download) — all are imported in ONE transaction.
        gemeinde_level: INSPIRE level (e.g. '4thOrder') of the administrative
        unit to be treated as the municipality (Gemeinde); if not given, the
        previous heuristic applies (second referenced unit = city, last = locality).
        allow_shrink: accept new data with far fewer addresses than before."""
        super().__init__()
        self.filepaths  = _as_list(filepaths)
        self.filepath   = self.filepaths[0] if self.filepaths else ''
        self.bundesland = bundesland
        self.gemeinde_level = gemeinde_level
        self.rebuild_lookup = rebuild_lookup
        self.announce       = announce      # False: called from the automatic download
        self.replace    = replace
        self.allow_shrink = allow_shrink
        self._cancelled = False
        self._resolver      = None
        self._admin_levels  = {}
        self._postal_codes  = {}

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._run()
        except _GmlBroken as e:
            self.finished.emit(False, str(e))
        except Exception as e:
            import traceback
            self.detail.emit(traceback.format_exc())
            self.finished.emit(False, f'Import fehlgeschlagen: {e}. {KEEP_MSG}')

    def _iter_members(self):
        """Yields all feature containers (gml:featureMember /
        wfs:member) of all input files in turn; the caller calls elem.clear().
        An XML error raises _GmlBroken (the whole import is aborted — partial
        data would replace the complete existing data)."""
        for path in self.filepaths:
            try:
                for event, elem in ET.iterparse(path, events=('end',)):
                    if _is_member(elem.tag):
                        yield elem
                    elif self._cancelled:
                        return
            except ET.ParseError as e:
                self.detail.emit(f'XML-Parse-Fehler in {path}: {e}')
                raise _GmlBroken(
                    f'Die Datei {os.path.basename(path)} ist beschädigt oder unvollständig '
                    f'(XML-Fehler: {e}). Der Import wurde abgebrochen – der bisherige '
                    'Datenbestand bleibt erhalten.') from e

    def _run(self):
        if len(self.filepaths) == 1:
            (self.log if self.announce else self.detail).emit(
                f'Lese {os.path.basename(self.filepath)} …')
        self.detail.emit(f'GML-Import: {len(self.filepaths)} Datei(en) in '
                         f'{os.path.dirname(self.filepath)}')

        # ── Pass 1: build lookup tables ──────────────────────────────────────
        self.progress.emit(0, 0, 'Lese Straßennamen und Orte …')
        street_names, admin_names = self._build_lookup_tables()
        if self._cancelled:
            self.finished.emit(False, CANCEL_MSG)
            return
        self.detail.emit(f'Pass 1: {_n(len(street_names))} Straßennamen, '
                         f'{_n(len(admin_names))} Verwaltungseinheiten, '
                         f'{_n(len(self._postal_codes))} Postleitzahlen.')

        if not street_names:
            self.finished.emit(False,
                'Die Datei enthält keine INSPIRE-Adressdaten (keine Straßennamen '
                'gefunden). Bitte prüfen, ob die richtige und vollständige Datei '
                'gewählt wurde.')
            return

        # DELETE and all INSERTs run in ONE transaction (commit only at the
        # very end, rollback on error/cancel) — shared write path AddressSink.
        sink = AddressSink(self.replace, self.rebuild_lookup, self.allow_shrink,
                           self.log.emit, self.detail.emit)
        try:
            sink.start(self.bundesland)

            # ── Pass 2: import Address features ───────────────────────────────
            if self.announce:
                self.log.emit('Importiere Adressen …')
            self._resolver = build_resolver_logged(log=self.log.emit, detail=self.detail.emit)
            # Use file size as an estimate for overall progress
            try:
                estimated_total = sum(os.path.getsize(p) for p in self.filepaths) // 250
            except Exception:
                estimated_total = 0
            self.progress.emit(0, estimated_total, 'Importiere Adressen …')
            n_skip = self._import_addresses(sink, street_names, admin_names, estimated_total)

            if self._cancelled:
                sink.rollback()
                self.finished.emit(False, CANCEL_MSG)
                return
            ok, msg = sink.finish(self._resolver)
            if not ok:
                self.finished.emit(False, msg)
                return
        except Exception:
            sink.rollback()
            raise               # run() reports the error; existing data is kept
        finally:
            sink.close()

        n_ok = sink.n_ok
        msg = f'Import abgeschlossen: {_n(n_ok)} Adressen importiert'
        if n_skip:
            msg += f' ({_n(n_skip)} Einträge ohne verwertbare Adresse übersprungen)'
        self.progress.emit(n_ok, n_ok, 'Fertig.')  # progress bar to 100%
        self.finished.emit(True, msg + '.')

    # ── Pass 1 ────────────────────────────────────────────────────────────────

    def _build_lookup_tables(self):
        """
        Reads all ThoroughfareName, AdminUnitName and PostalDescriptor features.
        street_names: {gml_id → street name}
        admin_names:  {gml_id → name}
        self._postal_codes: {gml_id → ad:postCode}
        """
        street_names = {}
        admin_names  = {}
        self._admin_levels = {}   # gml_id -> INSPIRE level ('4thOrder' …)
        self._postal_codes = {}
        tag_tf, tag_au = _tag('ad', 'ThoroughfareName'), _tag('ad', 'AdminUnitName')
        tag_pd, tag_pc = _tag('ad', 'PostalDescriptor'), _tag('ad', 'postCode')

        for elem in self._iter_members():
            if self._cancelled:
                return {}, {}

            tf = elem.find(f'.//{tag_tf}')
            if tf is not None:
                gml_id = tf.get(_GML_ID, '')
                name   = self._extract_geo_name(tf)
                if gml_id and name:
                    street_names[gml_id] = name

            au = elem.find(f'.//{tag_au}')
            if au is not None:
                gml_id = au.get(_GML_ID, '')
                name   = self._extract_geo_name(au)
                if gml_id and name:
                    admin_names[gml_id] = name
                    self._admin_levels[gml_id] = _level_of(au)

            pd = elem.find(f'.//{tag_pd}')
            if pd is not None:
                gml_id = pd.get(_GML_ID, '')
                pc = pd.find(f'.//{tag_pc}')
                if gml_id:
                    self._postal_codes[gml_id] = (pc.text or '').strip() if pc is not None else ''

            elem.clear()

        return street_names, admin_names

    def _extract_geo_name(self, elem):
        """Extracts the text value from a GeographicalName container
        (gn:spelling/gn:SpellingOfName/gn:text)."""
        text_elem = elem.find(f'.//{{{NS["gn"]}}}text')
        if text_elem is not None and text_elem.text:
            return text_elem.text.strip()
        return ''

    # ── Pass 2 ────────────────────────────────────────────────────────────────

    def _import_addresses(self, sink, street_names, admin_names, estimated_total=0):
        n_skip = 0
        n_feat = 0
        n_err_logged = 0
        seen_ids = set()     # addresses at tile edges appear twice
        n_dup = 0
        gem_cache = {}

        tag_address  = _tag('ad', 'Address')
        tag_pos      = _tag('gml', 'pos')
        tag_point    = _tag('gml', 'Point')
        tag_desig    = _tag('ad', 'designator')
        tag_loc_des  = _tag('ad', 'LocatorDesignator')
        tag_type     = _tag('ad', 'type')
        tag_component = _tag('ad', 'component')

        for elem in self._iter_members():
            if self._cancelled:
                break

            addr = elem.find(f'.//{tag_address}')
            if addr is None:
                elem.clear()
                continue
            addr_id = addr.get(_GML_ID, '')
            if addr_id:
                if addr_id in seen_ids:
                    n_dup += 1
                    elem.clear()
                    continue
                seen_ids.add(addr_id)

            n_feat += 1
            if n_feat % 20000 == 0:
                self.progress.emit(n_feat, estimated_total, f'{_n(sink.n_ok)} Adressen eingelesen …')

            # Parsing errors of a single feature skip only this feature;
            # writing (sink.add) is OUTSIDE this try — DB errors abort the import.
            try:
                row = self._parse_address(addr, street_names, admin_names, gem_cache,
                                          tag_pos, tag_point, tag_desig, tag_loc_des,
                                          tag_type, tag_component)
            except Exception as e:
                row = None
                if n_err_logged < 3:
                    n_err_logged += 1
                    self.detail.emit(f'Feature übersprungen ({type(e).__name__}: {e})')
            elem.clear()
            if row is None:
                n_skip += 1
                continue
            sink.add(*row)

        if n_dup:
            self.detail.emit(f'{_n(n_dup)} doppelte Adressen (gleiche gml:id) übersprungen.')
        return n_skip

    def _parse_address(self, addr, street_names, admin_names, gem_cache, tag_pos, tag_point,
                       tag_desig, tag_loc_des, tag_type, tag_component):
        """One ad:Address -> argument tuple for AddressSink.add, or None."""
        pos_elem = addr.find(f'.//{tag_pos}')
        if pos_elem is None or not pos_elem.text:
            return None
        coords = pos_elem.text.strip().split()
        if len(coords) < 2:
            return None
        x, y = float(coords[0]), float(coords[1])

        # EPSG from srsName (of the point, otherwise of gml:pos)
        pt_elem = addr.find(f'.//{tag_point}')
        srs = (pt_elem.get('srsName', '') if pt_elem is not None else '') or \
            pos_elem.get('srsName', '')
        epsg, swap = parse_srs_name(srs)
        if swap:                     # lat/lon axis order -> x = longitude
            x, y = y, x

        # House number and suffix
        hausnummer = ''
        hn_zusatz  = ''
        for loc_des in addr.findall(f'.//{tag_loc_des}'):
            type_elem = loc_des.find(tag_type)
            des_elem  = loc_des.find(tag_desig)
            if type_elem is None or des_elem is None:
                continue
            kind = _designator_kind(type_elem.get(_XLINK_HREF, ''))
            if kind == 'addressNumber':
                hausnummer = des_elem.text.strip() if des_elem.text else ''
            elif kind == 'addressNumberExtension':
                hn_zusatz = des_elem.text.strip() if des_elem.text else ''

        # Reference IDs: '#AdminUnitName_1' (file) or full URL
        # 'https://…GetFeatureById&ID=…#AdminUnitName_1' (WFS M-V) —
        # the part after the last '#' is always what counts.
        components = [ref_id(c.get(_XLINK_HREF, '')) for c in addr.findall(tag_component)]

        strasse = next((street_names[c] for c in components if c in street_names), '')
        if not strasse:
            return None

        plz = _plz_of(components, self._postal_codes)
        herkunft = None
        if not plz and self._resolver is not None:
            plz = self._resolver.lookup(x, y, epsg)
            herkunft = 'gebiet' if plz else None

        # Municipality hierarchy from the referenced administrative units
        # (resolved by feature type, whatever the id looks like).
        # INSPIRE Hamburg has 6 administrative levels:
        #   Index 0: Bundesland (Hamburg)
        #   Index 1: city of Hamburg  ← this is the "city" for searching
        #   Index 2-5: borough/locality (too specific as municipality)
        # We store index 1 as gemeinde (= Hamburg) and
        # the most specific one as kreis (= city district/Stadtteil).
        au_refs = [c for c in components if c in admin_names]
        gemeinde = ''
        ortsteil = ''
        if self.gemeinde_level:
            # Configured INSPIRE level (e.g. '4thOrder' = municipality in M-V)
            for ref in au_refs:
                if self._admin_levels.get(ref) == self.gemeinde_level:
                    gemeinde = admin_names[ref]
                    break
        if not gemeinde:
            if len(au_refs) >= 2:
                gemeinde = admin_names[au_refs[1]]      # index 1 = parent city
            elif len(au_refs) == 1:
                gemeinde = admin_names[au_refs[0]]
        if au_refs:
            ortsteil = admin_names[au_refs[-1]]         # most specific = locality
        clean = gem_cache.get(gemeinde)
        if clean is None:
            clean = gem_cache[gemeinde] = _clean_gemeinde(gemeinde)

        return (self.bundesland, ortsteil, clean, plz, strasse, hausnummer, hn_zusatz,
                epsg, x, y, herkunft)

    @staticmethod
    def preview_file(filepath, n=2):
        """Quick preview: reads the first n Address features."""
        results = []
        try:
            for event, elem in ET.iterparse(filepath, events=('end',)):
                if _is_member(elem.tag):
                    addr = elem.find(f'.//{_tag("ad", "Address")}')
                    if addr is not None:
                        pos = addr.find(f'.//{_tag("gml", "pos")}')
                        components = [
                            ref_id(c.get(_XLINK_HREF, ''))
                            for c in addr.findall(_tag('ad', 'component'))
                        ]
                        plz = _plz_of(components, {})
                        tf_ref = next((c for c in components
                                       if c.startswith('ThoroughfareName_')), '')
                        hn = ''
                        for ld in addr.findall(f'.//{_tag("ad", "LocatorDesignator")}'):
                            t = ld.find(_tag('ad', 'type'))
                            d = ld.find(_tag('ad', 'designator'))
                            if (t is not None and d is not None
                                    and _designator_kind(t.get(_XLINK_HREF, '')) == 'addressNumber'):
                                hn = (d.text or '').strip()
                                break
                        coords = pos.text.strip().split() if pos is not None and pos.text else ['?', '?']
                        results.append({
                            'hausnummer':   hn,
                            'plz':          plz,
                            'strasse_ref':  tf_ref,
                            'x':            coords[0] if len(coords) > 0 else '?',
                            'y':            coords[1] if len(coords) > 1 else '?',
                        })
                    elem.clear()
                if len(results) >= n:
                    break
        except Exception:
            pass
        return results
