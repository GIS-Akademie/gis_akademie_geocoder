# -*- coding: utf-8 -*-
"""
Convert an OpenStreetMap extract (.osm.pbf, e.g. Geofabrik „bayern-latest“)
into an address file in HK-BY format (24 columns, like the official HK-BY /
Sachsen data) — the regular HK-DE import then imports the file.

Built-in version of the former script osm_to_hkby.py; instead of osmium and
pyproj it uses GDAL (OSM driver, included in QGIS).

All OSM objects with addr:housenumber and addr:street (or addr:place) are
taken over: address points, buildings (point on surface) and lines.
  * House number lists („12;14“) yield one address per number.
  * Duplicate addresses (e.g. address point AND building with the same
    address) are merged — the first hit (point) is kept.
  * Municipality = addr:city; if missing, it is determined from the municipal
    boundaries in the same extract (boundary=administrative, admin_level 8,
    otherwise 7/6).
  * addr:suburb (locality/Ortsteil) goes into the column „kreis“ — the search
    index builds locality aliases from it.
  * Coordinates in ETRS89/UTM 32 (EPSG:25832).

Data license: ODbL 1.0, © OpenStreetMap contributors — not official.
"""

import os
import re

HK_BY_HEADER = [
    'nba', 'oid', 'qua', 'landschl', 'land', 'regbezschl', 'regbez',
    'kreisschl', 'kreis', 'gmdschl', 'gmd', 'ottschl', 'ott', 'strschl',
    'str', 'hnr', 'adz', 'zone', 'ostwert', 'nordwert', 'postplz',
    'postonm', 'postonmzus', 'postott',
]

_ADDR_KEYS = ['addr:housenumber', 'addr:street', 'addr:place', 'addr:postcode',
              'addr:city', 'addr:suburb', 'admin_level', 'boundary']

_LANDSCHL = {
    'Schleswig-Holstein': '01', 'Hamburg': '02', 'Niedersachsen': '03', 'Bremen': '04',
    'Nordrhein-Westfalen': '05', 'Hessen': '06', 'Rheinland-Pfalz': '07',
    'Baden-Württemberg': '08', 'Bayern': '09', 'Saarland': '10', 'Berlin': '11',
    'Brandenburg': '12', 'Mecklenburg-Vorpommern': '13', 'Sachsen': '14',
    'Sachsen-Anhalt': '15', 'Thüringen': '16',
}

_SEP_RE   = re.compile(r'\s*[;,]\s*')
_HNR_RE   = re.compile(r'^\s*(\d{1,5})\s*([A-Za-z]{0,2})\s*$')
_LEAD_RE  = re.compile(r'^\s*(\d{1,5})\s*(.*)$')


class OsmConvertError(Exception):
    """Message worded for end users."""


def gdal_available():
    try:
        from osgeo import ogr
        return ogr.GetDriverByName('OSM') is not None
    except Exception:
        return False


def _clean(v):
    return (v or '').replace(';', ',').replace('\r', ' ').replace('\n', ' ').strip()


def split_housenumbers(value):
    """'12' -> [('12','')], '12a' -> [('12','a')], '12;14' -> [('12',''),('14','')],
    '12-14' -> [('12','')], '3 1/2' -> [('3','1/2')]. Without a leading digit: []."""
    out = []
    for part in _SEP_RE.split(value or ''):
        if not part:
            continue
        m = _HNR_RE.match(part)
        if m:
            out.append((m.group(1), m.group(2)))
            continue
        m = _LEAD_RE.match(part)
        if not m:
            continue
        rest = m.group(2).strip()
        if rest.startswith('-') or rest.startswith('–'):
            rest = ''                    # range '12-14' -> first number
        out.append((m.group(1), _clean(rest)[:10]))
    return out


def _write_config(path):
    """GDAL's osmconf.ini with the addr:* keys as separate columns."""
    from osgeo import gdal
    src = gdal.FindFile('gdal', 'osmconf.ini')
    if not src:
        raise OsmConvertError('Die GDAL-Konfiguration für OpenStreetMap-Daten wurde nicht gefunden.')
    lines = open(src, encoding='utf-8').read().splitlines()
    out, section = [], None
    for ln in lines:
        s = ln.strip()
        if s.startswith('[') and s.endswith(']'):
            section = s[1:-1]
        if section in ('points', 'lines', 'multipolygons') and s.startswith('attributes='):
            have = [a for a in s[len('attributes='):].split(',') if a]
            ln = 'attributes=' + ','.join(have + [k for k in _ADDR_KEYS if k not in have])
        out.append(ln)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')


class _AdminAreas:
    """Municipal boundaries for addresses without addr:city (grid index + GEOS)."""

    CELL = 10000.0

    def __init__(self):
        self.areas = []       # (rang = rank, name, envelope, geometry)
        self.grid = {}

    def add(self, name, level, geom):
        rang = {'8': 3, '7': 2, '6': 1}.get(level, 0)
        if not rang or not name or geom is None or geom.IsEmpty():
            return
        x0, x1, y0, y1 = geom.GetEnvelope()
        idx = len(self.areas)
        self.areas.append((rang, name, (x0, x1, y0, y1), geom))
        for gx in range(int(x0 // self.CELL), int(x1 // self.CELL) + 1):
            for gy in range(int(y0 // self.CELL), int(y1 // self.CELL) + 1):
                self.grid.setdefault((gx, gy), []).append(idx)

    def lookup(self, x, y):
        from osgeo import ogr
        cands = [self.areas[i] for i in self.grid.get((int(x // self.CELL), int(y // self.CELL)), ())]
        if not cands:
            return ''
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(x, y)
        for rang, name, (x0, x1, y0, y1), geom in sorted(cands, key=lambda a: -a[0]):
            if x0 <= x <= x1 and y0 <= y <= y1 and geom.Contains(pt):
                return name
        return ''


def osm_to_hkby(pbf_path, out_path, bundesland, progress=None, cancel=None, detail=None):
    """Convert pbf_path into an HK-BY file out_path. Returns a dict with
    adressen, doppelt, gemeinde_ergaenzt, ohne_gemeinde, objekte.
    progress(done, total, text); cancel() -> True aborts (Cancelled)."""
    from osgeo import gdal, ogr, osr
    from .source_downloader import Cancelled

    _detail = detail or (lambda m: None)
    if ogr.GetDriverByName('OSM') is None:
        raise OsmConvertError('Diese QGIS-Installation kann keine OpenStreetMap-Dateien lesen '
                              '(GDAL ohne OSM-Treiber).')
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    cfg = os.path.join(out_dir, 'osmconf_geocoder.ini')
    _write_config(cfg)

    # Temporary files of the OSM driver (node index, several GB for whole
    # Bundesländer) go to the working folder instead of the system temp
    old_tmp = gdal.GetConfigOption('CPL_TMPDIR')
    gdal.SetConfigOption('CPL_TMPDIR', out_dir)
    try:
        ds = gdal.OpenEx(pbf_path, gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=['OSM'],
                         open_options=[f'CONFIG_FILE={cfg}'])
    finally:
        gdal.SetConfigOption('CPL_TMPDIR', old_tmp)
    if ds is None:
        raise OsmConvertError(f'Die Datei {os.path.basename(pbf_path)} ist kein lesbarer '
                              'OpenStreetMap-Extrakt (.osm.pbf).')

    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(25832)
    try:
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    tr = osr.CoordinateTransformation(src, dst)

    landschl = _LANDSCHL.get(bundesland, '')
    seen = set()
    pending = []              # rows without municipality -> after the boundaries
    admin = _AdminAreas()
    stats = {'adressen': 0, 'doppelt': 0, 'gemeinde_ergaenzt': 0, 'ohne_gemeinde': 0,
             'objekte': 0}
    field_idx = {}
    last_pct = -1

    def _row(oid, gmd, ott, street, hnr, adz, x, y, plz, city):
        return ['N', oid[:16], '', landschl, bundesland, '', '', '', ott, '', gmd, '', ott, '',
                street, hnr, adz, '32', f'{x:.2f}', f'{y:.2f}', plz, city, '', ott]

    with open(out_path, 'w', encoding='utf-8', newline='') as fh:
        fh.write(';'.join(HK_BY_HEADER) + '\r\n')
        n = 0
        while True:
            res = ds.GetNextFeature(include_layer=True, include_pct=True)
            feat, lyr, pct = res if isinstance(res, (list, tuple)) else (res, None, None)
            if feat is None:
                break
            n += 1
            if n % 20000 == 0:
                if cancel and cancel():
                    raise Cancelled()
                if pct is not None and int(pct * 100) != last_pct:
                    last_pct = int(pct * 100)
                    if progress:
                        progress(last_pct, 100, f'OpenStreetMap-Daten werden umgewandelt … {last_pct} %')
            lname = lyr.GetName() if lyr is not None else ''
            if lname not in ('points', 'lines', 'multipolygons'):
                continue
            idx = field_idx.get(lname)
            if idx is None:
                defn = lyr.GetLayerDefn()
                # The OSM driver writes 'addr:street' as column 'addr_street'
                idx = {k: defn.GetFieldIndex(k.replace(':', '_'))
                       for k in _ADDR_KEYS + ['osm_id', 'osm_way_id', 'name']}
                field_idx[lname] = idx

            def val(key):
                i = idx.get(key, -1)
                return feat.GetFieldAsString(i) if i >= 0 and feat.IsFieldSet(i) else ''

            if lname == 'multipolygons' and val('boundary') == 'administrative' \
                    and val('admin_level') in ('6', '7', '8'):
                g = feat.GetGeometryRef()
                if g is not None:
                    g = g.Clone()
                    g.Transform(tr)
                    admin.add(val('name'), val('admin_level'), g)

            hn = val('addr:housenumber')
            if not hn:
                continue
            street = _clean(val('addr:street') or val('addr:place'))
            if not street:
                continue
            g = feat.GetGeometryRef()
            if g is None:
                continue
            try:
                if g.GetGeometryType() not in (ogr.wkbPoint, ogr.wkbPoint25D):
                    g = g.PointOnSurface() if lname == 'multipolygons' else g.Centroid()
                    if g is None:
                        continue
                else:
                    g = g.Clone()
                g.Transform(tr)
                x, y = g.GetX(), g.GetY()
            except Exception:
                continue
            stats['objekte'] += 1
            plz = _clean(val('addr:postcode'))
            city = _clean(val('addr:city'))
            ott = _clean(val('addr:suburb'))
            oid = 'OSM' + {'points': 'N', 'lines': 'W', 'multipolygons': 'A'}[lname] + \
                  (val('osm_id') or val('osm_way_id'))
            for hnr, adz in split_housenumbers(hn):
                key = hash((street.lower(), hnr, adz.lower(), plz, city.lower()))
                if key in seen:
                    stats['doppelt'] += 1
                    continue
                seen.add(key)
                row = _row(oid, city, ott, street, hnr, _clean(adz), x, y, plz, city)
                if city:
                    fh.write(';'.join(row) + '\r\n')
                    stats['adressen'] += 1
                else:
                    pending.append((x, y, row))

        # Addresses without addr:city: municipality from the boundaries
        _detail(f'OSM: {len(admin.areas)} Verwaltungsgrenzen, {len(pending)} Adressen ohne addr:city')
        for i, (x, y, row) in enumerate(pending):
            if i % 5000 == 0:
                if cancel and cancel():
                    raise Cancelled()
                if progress:
                    progress(i, len(pending), f'Gemeinde für Adressen ohne Ortsangabe ermitteln … '
                                              f'{i * 100 // max(len(pending), 1)} %')
            name = admin.lookup(x, y)
            if name:
                row[10] = row[21] = name
                stats['gemeinde_ergaenzt'] += 1
            else:
                stats['ohne_gemeinde'] += 1
            fh.write(';'.join(row) + '\r\n')
            stats['adressen'] += 1
    ds = None
    try:
        os.remove(cfg)
    except OSError:
        pass
    if progress:
        progress(100, 100, 'OpenStreetMap-Daten umgewandelt.')
    _detail(f'OSM-Umwandlung: {stats}')
    if not stats['adressen']:
        raise OsmConvertError('Im OpenStreetMap-Extrakt wurden keine Adressen gefunden.')
    return stats
