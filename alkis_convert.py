# -*- coding: utf-8 -*-
"""
Convert ALKIS vector data (one GeoPackage per district/Kreis, e.g. LGLN
OpenGeoData Niedersachsen) into address rows in HK-BY format (24 columns) —
the regular HK-DE import then imports the file.

Built-in version of the former scripts download_gebaeude_ni.py,
merge_gebaeude_ni.py and gebaeude_zu_hkde.py. Differences:
  * the municipality comes from the parcels (Flurstücke) of the same file
    (field gem__bez) — no separate municipality-area file needed;
  * the plugin fills in the postcode from the bundled postcode areas;
  * per district the zip is downloaded, converted and deleted right away.

Municipality assignment: the parcels are sorted into a grid (250 m). If a cell
contains only one municipality, it applies without a geometry test; only cells
along municipal boundaries are checked with point-in-polygon (GEOS).
License of the LGLN data: CC BY 4.0, „© GeoBasis-DE/LGLN <Jahr>, Daten geändert“.
"""

import contextlib
import os
import re
import zipfile

from .osm_convert import (HK_BY_HEADER, split_housenumbers, _LANDSCHL, OsmConvertError,
                          _clean, hk_by_row)

DEFAULTS = {
    'gebaeude_layer':   'gebaeude',
    'strasse':          'bez',
    'hausnummer':       'hnr',
    'flurstueck_layer': 'flurstuecke',
    'gemeinde_feld':    'gem__bez',
    'epsg':             25832,      # the LGLN GeoPackages carry no coordinate system
}
CELL = 250.0

# „Emden, Stadt“, „Hannover, Landeshauptstadt“, „Bad Bentheim, Stadt“ → name without suffix
_ZUSATZ_RE = re.compile(r',\s*[^,]*$')


class AlkisConvertError(OsmConvertError):
    """Message worded for end users."""


def gemeinde_name(bez):
    """ALKIS municipality name without the suffix after the comma."""
    bez = (bez or '').strip()
    return _ZUSATZ_RE.sub('', bez).strip() if ',' in bez else bez


def kreis_aus_dateiname(name):
    """'lkr_03453_Cloppenburg_kon.gpkg.zip' → 'Cloppenburg',
    'lkr_03402_Stadt_Emden_kon…' → 'Stadt Emden'."""
    base = os.path.basename(name)
    m = re.match(r'^lkr_\d+_(.+?)_kon', base)
    return (m.group(1) if m else os.path.splitext(base)[0]).replace('_', ' ')


def _gpkg_path(path):
    if zipfile.is_zipfile(path):
        names = [n for n in zipfile.ZipFile(path).namelist() if n.lower().endswith('.gpkg')]
        if not names:
            raise AlkisConvertError(f'Im Archiv {os.path.basename(path)} ist kein GeoPackage.')
        return f'/vsizip/{path}/{names[0]}'
    return path


def _zone_of(epsg):
    """HK zone column for the target EPSG: 25832 -> '32', 25833 -> '33';
    other codes are written in full (the import reads both forms)."""
    epsg = int(epsg)
    return str(epsg - 25800) if 25801 <= epsg <= 25860 else str(epsg)


def _same_crs(a, b):
    """True if both spatial references are the same — or one is unknown (the
    LGLN GeoPackages carry no coordinate system; then both layers share it)."""
    if a is None or b is None:
        return True
    try:
        return bool(a.IsSame(b))
    except Exception:
        return a.ExportToWkt() == b.ExportToWkt()


class _GemeindeRaster:
    """Municipality per point from the parcels (see module docstring)."""

    def __init__(self, layer, feld, cancel=None):
        from osgeo import ogr
        from .source_downloader import Cancelled
        idx = layer.GetLayerDefn().GetFieldIndex(feld)
        if idx < 0:
            raise AlkisConvertError(f'Die Flurstücke enthalten kein Feld „{feld}“.')
        # Pass 1: names per cell (via the bounding boxes of the parcels)
        self.cells = {}
        boxes = []
        for n, f in enumerate(layer):
            if cancel and n % 50000 == 0 and cancel():
                raise Cancelled()
            g = f.GetGeometryRef()
            if g is None:
                continue
            name = gemeinde_name(f.GetFieldAsString(idx))
            x0, x1, y0, y1 = g.GetEnvelope()
            boxes.append((f.GetFID(), name, x0, x1, y0, y1))
            for c in self._cells(x0, x1, y0, y1):
                self.cells.setdefault(c, set()).add(name)
        self.mixed = {c for c, names in self.cells.items() if len(names) > 1}
        # Pass 2: keep geometries only for parcels in boundary cells
        self.polys = {}
        need = {}
        for fid, name, x0, x1, y0, y1 in boxes:
            cs = [c for c in self._cells(x0, x1, y0, y1) if c in self.mixed]
            if cs:
                need[fid] = (name, (x0, x1, y0, y1), cs)
        for fid, (name, env, cs) in need.items():
            f = layer.GetFeature(fid)
            g = f.GetGeometryRef() if f is not None else None
            if g is None:
                continue
            entry = (name, env, g.Clone())
            for c in cs:
                self.polys.setdefault(c, []).append(entry)
        self._pt = ogr.Geometry(ogr.wkbPoint)

    @staticmethod
    def _cells(x0, x1, y0, y1):
        for gx in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for gy in range(int(y0 // CELL), int(y1 // CELL) + 1):
                yield gx, gy

    def lookup(self, x, y):
        c = (int(x // CELL), int(y // CELL))
        names = self.cells.get(c)
        if not names:
            return ''
        if len(names) == 1:
            return next(iter(names))
        self._pt.SetPoint_2D(0, x, y)
        for name, (x0, x1, y0, y1), g in self.polys.get(c, ()):
            if x0 <= x <= x1 and y0 <= y <= y1 and g.Contains(self._pt):
                return name
        return ''


def _building_points(g, tr_lookup, tr):
    """(x, y in the parcels' coordinate system for the municipality lookup,
    x, y in the target system) of a building, or None without a usable point."""
    p = g.PointOnSurface() if g.GetDimension() == 2 else g.Centroid()
    if p is None or p.IsEmpty():
        return None
    if tr_lookup is not None:
        q = p.Clone()
        q.Transform(tr_lookup)
        lx, ly = q.GetX(), q.GetY()
    else:
        lx, ly = p.GetX(), p.GetY()
    if tr is not None:
        p.Transform(tr)
    return lx, ly, p.GetX(), p.GetY()


def alkis_to_rows(path, out_fh, bundesland, kreis='', cfg=None, cancel=None, seen=None,
                  progress=None, detail=None):
    """Append buildings with an address from an ALKIS GeoPackage (or zip) as
    HK-BY rows to out_fh (without header row). seen: shared set for removing
    duplicate addresses across several districts. Returns a dict with adressen,
    doppelt, ohne_gemeinde, gebaeude."""
    from osgeo import ogr, osr
    from .source_downloader import Cancelled
    cfg = dict(DEFAULTS, **(cfg or {}))
    _detail = detail or (lambda m: None)
    ds = ogr.Open(_gpkg_path(path))
    if ds is None:
        raise AlkisConvertError(f'Die Datei {os.path.basename(path)} ist kein lesbares GeoPackage.')
    geb = ds.GetLayerByName(cfg['gebaeude_layer'])
    flst = ds.GetLayerByName(cfg['flurstueck_layer'])
    if geb is None or flst is None:
        raise AlkisConvertError(f'In {os.path.basename(path)} fehlen die Layer '
                                f'„{cfg["gebaeude_layer"]}“ oder „{cfg["flurstueck_layer"]}“.')
    if progress:
        progress(0, 0, 'Gemeinden aus den Flurstücken …')
    raster = _GemeindeRaster(flst, cfg['gemeinde_feld'], cancel)

    def _traditional(s):
        with contextlib.suppress(AttributeError):
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return s

    # Output: building points in the target EPSG. Municipality lookup: in the
    # CRS of the PARCELS (the raster is built from their untransformed
    # coordinates) — the point is looked up before the output transformation.
    tr = None
    tr_lookup = None
    srs = geb.GetSpatialRef()
    fl_srs = flst.GetSpatialRef()
    if srs is not None and srs.GetAuthorityCode(None) not in (None, str(cfg['epsg'])):
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(int(cfg['epsg']))
        tr = osr.CoordinateTransformation(_traditional(srs.Clone()), _traditional(dst))
    if not _same_crs(srs, fl_srs):
        tr_lookup = osr.CoordinateTransformation(_traditional(srs.Clone()),
                                                 _traditional(fl_srs.Clone()))
    zone = _zone_of(cfg['epsg'])

    defn = geb.GetLayerDefn()
    i_str, i_hnr = defn.GetFieldIndex(cfg['strasse']), defn.GetFieldIndex(cfg['hausnummer'])
    if i_str < 0 or i_hnr < 0:
        raise AlkisConvertError(f'Die Gebäude enthalten keine Felder „{cfg["strasse"]}“/'
                                f'„{cfg["hausnummer"]}“.')
    keep = {cfg['strasse'], cfg['hausnummer']}
    geb.SetIgnoredFields([defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())
                          if defn.GetFieldDefn(i).GetName() not in keep])
    geb.SetAttributeFilter(f"{cfg['hausnummer']} IS NOT NULL AND {cfg['hausnummer']} <> ''")
    total = geb.GetFeatureCount()
    seen = seen if seen is not None else set()
    landschl = _LANDSCHL.get(bundesland, '')
    st = {'adressen': 0, 'doppelt': 0, 'ohne_gemeinde': 0, 'gebaeude': 0}
    for n, f in enumerate(geb):
        if n % 20000 == 0:
            if cancel and cancel():
                raise Cancelled()
            if progress and total:
                progress(n, total, f'{n:,} von {total:,} Gebäuden …'.replace(',', '.'))
        strasse = _clean(f.GetFieldAsString(i_str))
        hn = f.GetFieldAsString(i_hnr)
        g = f.GetGeometryRef()
        if not strasse or not hn or g is None:
            continue
        pts = None
        with contextlib.suppress(Exception):      # broken geometry: building is skipped
            pts = _building_points(g, tr_lookup, tr)
        if pts is None:
            continue
        lx, ly, x, y = pts
        st['gebaeude'] += 1
        gmd = raster.lookup(lx, ly)
        if not gmd:
            st['ohne_gemeinde'] += 1
        for hnr, adz in split_housenumbers(hn):
            adz = _clean(adz)
            key = hash((strasse.lower(), hnr, adz.lower(), gmd))
            if key in seen:
                st['doppelt'] += 1
                continue
            seen.add(key)
            row = hk_by_row(landschl, bundesland, kreis, gmd, strasse, hnr, adz, zone, x, y, '',
                            gmd, ident=f.GetFID(), qua='A')
            out_fh.write(';'.join(row) + '\r\n')
            st['adressen'] += 1
    ds = None
    _detail(f'ALKIS {kreis}: {st}')
    return st


def write_header(out_fh):
    out_fh.write(';'.join(HK_BY_HEADER) + '\r\n')
