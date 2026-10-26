# -*- coding: utf-8 -*-
"""
Import worker for the INSPIRE GML format (Hamburg and other Bundesländer).

Structure:
  - ad:ThoroughfareName  → street name (attribute: text)
  - ad:PostalDescriptor  → postcode/PLZ (extractable from gml:id: PostalDescriptor_22359 → 22359)
  - ad:AdminUnitName     → administrative unit (most specific = district/municipality)
  - ad:Address           → house number, coordinates, references to the above

Strategy: two-pass streaming with xml.etree.ElementTree iterparse.
  Pass 1: load all ThoroughfareName and AdminUnitName blocks into dicts
  Pass 2: process Address features and resolve references
"""

import sqlite3
import os
import re

from qgis.PyQt.QtCore import QObject, pyqtSignal

from . import local_geocoder as _lc
from .local_geocoder import init_db, normalize, _core_gemeinde
from .plz_resolver import build_resolver_logged

BATCH_SIZE = 5000


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
    href = lvl.get(f'{{{NS["xlink"]}}}href', '') or ''
    val  = href.rstrip('/').rsplit('/', 1)[-1] if href else (lvl.text or '')
    return val.strip()


class GmlImportWorker(QObject):

    progress  = pyqtSignal(int, int, str)
    log       = pyqtSignal(str)             # log for the user
    detail    = pyqtSignal(str)             # technical details (QGIS message log)
    finished  = pyqtSignal(bool, str)

    def __init__(self, filepath, bundesland, replace=True, gemeinde_level=None,
                 rebuild_lookup=True, announce=True):
        """filepath: one GML file or a LIST of files (e.g. the pages of a
        WFS download) — all are imported in ONE transaction.
        gemeinde_level: INSPIRE level (e.g. '4thOrder') of the administrative
        unit to be treated as the municipality (Gemeinde); if not given, the
        previous heuristic applies (second referenced unit = city, last = locality)."""
        super().__init__()
        self.filepaths  = [filepath] if isinstance(filepath, str) else list(filepath)
        self.filepath   = self.filepaths[0]
        self.bundesland = bundesland
        self.gemeinde_level = gemeinde_level
        self.rebuild_lookup = rebuild_lookup
        self.announce       = announce      # False: called from the automatic download
        self.replace    = replace
        self._cancelled = False
        self._plz_source    = None
        self._plz_field     = 'plz'
        self._plz_layername = None
        self._resolver      = None

    def set_plz_layer(self, source, field='plz', layername=None):
        """Optional postcode (PLZ) polygon layer; missing postcodes are filled in during import."""
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

    def _iter_members(self):
        """Yields all feature containers (gml:featureMember /
        wfs:member) of all input files in turn; the caller calls elem.clear()."""
        import xml.etree.ElementTree as ET
        for path in self.filepaths:
            for event, elem in ET.iterparse(path, events=('end',)):
                if _is_member(elem.tag):
                    yield elem
                elif self._cancelled:
                    return

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
            self.finished.emit(False, 'Import abgebrochen – der bisherige '
                                      'Datenbestand bleibt erhalten.')
            return
        self.detail.emit(f'Pass 1: {_n(len(street_names))} Straßennamen, '
                         f'{_n(len(admin_names))} Verwaltungseinheiten.')

        if not street_names:
            self.finished.emit(False,
                'Die Datei enthält keine INSPIRE-Adressdaten (keine Straßennamen '
                'gefunden). Bitte prüfen, ob die richtige und vollständige Datei '
                'gewählt wurde.')
            return

        # ── Database connection ───────────────────────────────────────────────
        conn = sqlite3.connect(_lc.DB_PATH)
        init_db(conn)

        # DELETE and all INSERTs run in ONE transaction (commit only at the
        # very end, rollback on error/cancel) — as in the HK-DE import. Previously
        # the DELETE and each batch were committed immediately; a cancel
        # left the old data deleted plus partial data.
        try:
            if self.replace:
                self.log.emit(f'Die bisherigen Adressen für {self.bundesland} werden ersetzt.')
                conn.execute("DELETE FROM addresses WHERE bundesland = ?",
                             (self.bundesland,))

            # ── Pass 2: import Address features ───────────────────────────────
            if self.announce:
                self.log.emit('Importiere Adressen …')
            self._resolver = build_resolver_logged(
                self._plz_source, self._plz_field, self._plz_layername, self.log.emit,
                self.detail.emit)
            # Use file size as an estimate for overall progress
            try:
                estimated_total = sum(os.path.getsize(p) for p in self.filepaths) // 250
            except Exception:
                estimated_total = 0
            self.progress.emit(0, estimated_total, 'Importiere Adressen …')
            n_ok, n_skip = self._import_addresses(conn, street_names, admin_names,
                                                   estimated_total)

            if self._cancelled:
                conn.rollback()
                self.finished.emit(False, 'Import abgebrochen – der bisherige '
                                          'Datenbestand bleibt erhalten.')
                return

            if n_ok == 0:
                # Empty or wrong file (e.g. maintenance page instead of data): replace
                # nothing — otherwise the state's existing data would be deleted
                conn.rollback()
                conn.close()
                self.finished.emit(False, 'In der Datei wurden keine Adressen gefunden – der bisherige '
                                          'Datenbestand bleibt erhalten.')
                return
            if self._resolver is not None:
                st = self._resolver.stats()
                if st["matched"] or st["missed"]:   # nothing to fill in → no message
                    self.log.emit(f"PLZ ergänzt für {_n(st['matched'])} Adressen"
                                  + (f"; {_n(st['missed'])} liegen außerhalb der PLZ-Flächen."
                                     if st['missed'] else '.'))
            _lc.record_plz_meta(conn, self.bundesland,
                                _lc.resolver_info(self._resolver, self._plz_source,
                                                  self._plz_field, self._plz_layername),
                                self.replace)

            # Municipality/locality index for the new data (part of the same
            # transaction), then ONE atomic commit including the DELETE.
            if self.rebuild_lookup:
                _lc.rebuild_lookup_tables(conn, self.log.emit)
            conn.commit()
        except Exception:
            conn.rollback()
            raise               # run() reports the error; existing data is kept
        finally:
            conn.close()

        msg = f'Import abgeschlossen: {_n(n_ok)} Adressen importiert'
        if n_skip:
            msg += f' ({_n(n_skip)} Einträge ohne verwertbare Adresse übersprungen)'
        self.progress.emit(n_ok, n_ok, 'Fertig.')  # progress bar to 100%
        self.finished.emit(True, msg + '.')

    # ── Pass 1 ────────────────────────────────────────────────────────────────

    def _build_lookup_tables(self):
        """
        Reads all ThoroughfareName and AdminUnitName features into dicts.
        street_names: {gml_id → street name}
        admin_names:  {gml_id → name}
        """
        import xml.etree.ElementTree as ET

        street_names = {}
        admin_names  = {}
        self._admin_levels = {}   # gml_id -> INSPIRE level ('4thOrder' …)

        try:
            for elem in self._iter_members():
                if self._cancelled:
                    return {}, {}

                # ThoroughfareName
                tf = elem.find(f'.//{_tag("ad", "ThoroughfareName")}')
                if tf is not None:
                    gml_id = tf.get(f'{{{NS["gml"]}}}id', '')
                    name   = self._extract_geo_name(tf)
                    if gml_id and name:
                        street_names[gml_id] = name

                # AdminUnitName
                au = elem.find(f'.//{_tag("ad", "AdminUnitName")}')
                if au is not None:
                    gml_id = au.get(f'{{{NS["gml"]}}}id', '')
                    name   = self._extract_geo_name(au)
                    if gml_id and name:
                        admin_names[gml_id] = name
                        self._admin_levels[gml_id] = _level_of(au)

                elem.clear()

        except ET.ParseError as e:
            self.log.emit('Warnung: Die Datei ist beschädigt oder unvollständig – es werden '
                          'nur die lesbaren Adressen übernommen.')
            self.detail.emit(f'XML-Parse-Fehler in Pass 1: {e}')

        return street_names, admin_names

    def _extract_geo_name(self, elem):
        """Extracts the text value from a GeographicalName container
        (gn:spelling/gn:SpellingOfName/gn:text)."""
        text_elem = elem.find(f'.//{{{NS["gn"]}}}text')
        if text_elem is not None and text_elem.text:
            return text_elem.text.strip()
        return ''

    # ── Pass 2 ────────────────────────────────────────────────────────────────

    def _import_addresses(self, conn, street_names, admin_names, estimated_total=0):
        batch  = []
        n_ok   = 0
        n_skip = 0
        n_feat = 0
        n_err_logged = 0
        seen_ids = set()     # addresses at tile edges appear twice
        n_dup = 0

        tag_address  = _tag('ad', 'Address')
        tag_pos      = _tag('gml', 'pos')
        tag_desig    = _tag('ad', 'designator')
        tag_loc_des  = _tag('ad', 'LocatorDesignator')
        tag_type     = _tag('ad', 'type')
        tag_component = _tag('ad', 'component')
        xlink_href   = f'{{{NS["xlink"]}}}href'

        for elem in self._iter_members():
            if self._cancelled:
                break

            addr = elem.find(f'.//{tag_address}')
            if addr is None:
                elem.clear()
                continue
            addr_id = addr.get(f'{{{NS["gml"]}}}id', '')
            if addr_id:
                if addr_id in seen_ids:
                    n_dup += 1
                    elem.clear()
                    continue
                seen_ids.add(addr_id)

            n_feat += 1
            if n_feat % 20000 == 0:
                self.progress.emit(n_feat, estimated_total, f'{_n(n_ok)} Adressen eingelesen …')

            try:
                # Coordinates
                pos_elem = addr.find(f'.//{tag_pos}')
                if pos_elem is None or not pos_elem.text:
                    n_skip += 1
                    elem.clear()
                    continue
                coords = pos_elem.text.strip().split()
                if len(coords) < 2:
                    n_skip += 1
                    elem.clear()
                    continue
                x, y = float(coords[0]), float(coords[1])

                # EPSG from srsName
                epsg = 25832
                pt_elem = addr.find(f'.//{_tag("gml", "Point")}')
                if pt_elem is not None:
                    srs = pt_elem.get('srsName', '')
                    # 'EPSG:25832' as well as the usual INSPIRE form
                    # 'urn:ogc:def:crs:EPSG::25832' (two colons) —
                    # the latter was previously not recognized, it always stayed 25832.
                    m = re.search(r'EPSG:{1,2}(\d+)', srs, re.IGNORECASE)
                    if m:
                        epsg = int(m.group(1))

                # House number and suffix
                hausnummer = ''
                hn_zusatz  = ''
                for loc_des in addr.findall(f'.//{tag_loc_des}'):
                    type_elem = loc_des.find(tag_type)
                    des_elem  = loc_des.find(tag_desig)
                    if type_elem is None or des_elem is None:
                        continue
                    href = type_elem.get(xlink_href, '')
                    if 'addressNumber/' in href or href.endswith('addressNumber'):
                        hausnummer = des_elem.text.strip() if des_elem.text else ''
                    elif 'addressNumberExtension' in href:
                        hn_zusatz = des_elem.text.strip() if des_elem.text else ''

                # Resolve references
                # Reference IDs: '#AdminUnitName_1' (file) or full URL
                # 'https://…GetFeatureById&ID=…#AdminUnitName_1' (WFS M-V) —
                # the part after the last '#' is always what counts.
                components = [
                    ref_id(c.get(xlink_href, ''))
                    for c in addr.findall(tag_component)
                ]

                # Street name
                strasse = ''
                for comp_id in components:
                    if comp_id in street_names:
                        strasse = street_names[comp_id]
                        break

                # Postcode (PLZ) from PostalDescriptor ID (PostalDescriptor_22359 → 22359)
                plz = ''
                for comp_id in components:
                    if comp_id.startswith('PostalDescriptor_'):
                        plz = comp_id.replace('PostalDescriptor_', '')
                        break

                # Fill in missing postcodes from the postcode areas
                herkunft = None
                if not plz and self._resolver is not None:
                    plz = self._resolver.lookup(x, y, epsg)
                    herkunft = 'gebiet' if plz else None

                # Build the municipality hierarchy from AdminUnitName references.
                # INSPIRE Hamburg has 6 administrative levels:
                #   Index 0: Bundesland (Hamburg)
                #   Index 1: city of Hamburg  ← this is the "city" for searching
                #   Index 2-5: borough/locality (too specific as municipality)
                # We store index 1 as gemeinde (= Hamburg) and
                # the most specific one as kreis (= city district/Stadtteil).
                au_refs = [c for c in components
                           if c.startswith('AdminUnitName_') and c in admin_names]
                gemeinde = ''
                ortsteil  = ''
                if self.gemeinde_level:
                    # Configured INSPIRE level (e.g. '4thOrder' = municipality in M-V)
                    for ref in au_refs:
                        if self._admin_levels.get(ref) == self.gemeinde_level:
                            gemeinde = admin_names[ref]
                            break
                if not gemeinde:
                    if len(au_refs) >= 2:
                        # Index 1 = parent city (Hamburg)
                        gemeinde = admin_names[au_refs[1]]
                    elif len(au_refs) == 1:
                        gemeinde = admin_names[au_refs[0]]
                if au_refs:
                    # Most specific = last entry = locality/city district
                    ortsteil = admin_names[au_refs[-1]]

                if not strasse:
                    n_skip += 1
                    elem.clear()
                    continue

                batch.append((
                    self.bundesland,
                    ortsteil,        # kreis = city district (Stadtteil)
                    gemeinde,
                    plz,
                    strasse,
                    hausnummer,
                    hn_zusatz,
                    epsg,
                    x,
                    y,
                    normalize(strasse),
                    _core_gemeinde(gemeinde),
                    herkunft,
                ))
                n_ok += 1

                if len(batch) >= BATCH_SIZE:
                    self._flush(conn, batch)
                    batch = []

            except Exception as e:
                n_skip += 1
                if n_err_logged < 3:
                    n_err_logged += 1
                    self.detail.emit(f'Feature übersprungen ({type(e).__name__}: {e})')

            elem.clear()

        if batch:
            self._flush(conn, batch)

        if n_dup:
            self.detail.emit(f'{_n(n_dup)} doppelte Adressen (gleiche gml:id) übersprungen.')
        return n_ok, n_skip

    def _flush(self, conn, batch):
        conn.executemany("""
            INSERT INTO addresses
              (bundesland, kreis, gemeinde, plz, strasse, hausnummer,
               hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm, plz_herkunft)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, batch)
        # NO commit per batch — the whole transaction is committed in _run.

    def _detect_encoding(self):
        try:
            with open(self.filepath, 'rb') as f:
                start = f.read(200).decode('ascii', errors='replace')
            m = re.search(r'encoding=["\']([^"\']+)["\']', start)
            if m:
                return m.group(1)
        except Exception:
            pass
        return 'utf-8'

    @staticmethod
    def preview_file(filepath, n=2):
        """Quick preview: reads the first n Address features."""
        import xml.etree.ElementTree as ET
        results = []
        xlink_href = f'{{{NS["xlink"]}}}href'
        try:
            for event, elem in ET.iterparse(filepath, events=('end',)):
                if _is_member(elem.tag):
                    addr = elem.find(f'.//{_tag("ad", "Address")}')
                    if addr is not None:
                        pos = addr.find(f'.//{_tag("gml", "pos")}')
                        components = [
                            ref_id(c.get(xlink_href, ''))
                            for c in addr.findall(_tag('ad', 'component'))
                        ]
                        plz = next((c.replace('PostalDescriptor_', '')
                                    for c in components
                                    if c.startswith('PostalDescriptor_')), '')
                        tf_ref = next((c for c in components
                                       if c.startswith('ThoroughfareName_')), '')
                        hn = ''
                        for ld in addr.findall(f'.//{_tag("ad", "LocatorDesignator")}'):
                            t = ld.find(_tag('ad', 'type'))
                            d = ld.find(_tag('ad', 'designator'))
                            if t is not None and 'addressNumber/' in t.get(xlink_href,'') and d is not None:
                                hn = d.text or ''
                                break
                        coords = pos.text.strip().split() if pos is not None and pos.text else ['?','?']
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
