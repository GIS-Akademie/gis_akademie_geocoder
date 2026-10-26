# -*- coding: utf-8 -*-
"""
Postcodes (PLZ) from postcode areas (point-in-polygon).

The plugin ships the postcode areas of Germany (data/plz_gebiete.geojson,
taken directly from the OpenStreetMap boundaries boundary=postal_code, ODbL;
generated with tools/build_plz_gebiete.py from the Geofabrik Germany extract).
Missing postcodes are thus filled in without any further action:

  * when importing any address file (all formats), for addresses without PLZ,
  * in the post-processing step ergaenze_plz() for already imported addresses
    without PLZ and — if the postcode areas have changed — for all PLZ
    previously derived from areas.

PLZ from the official source always take precedence and are never overwritten.
The origin is stored in addresses.plz_herkunft ('gebiet' = from postcode area,
empty = from the source).

Instead of the bundled areas, a custom file can be set
(set_override, e.g. licensed postcode areas from Deutsche Post).

Two computation paths with the same interface (lookup(x, y, epsg) -> PLZ or ''):
  * PlzResolver   — with QGIS (QgsSpatialIndex + prepared geometries),
                    roughly 40,000–60,000 addresses per second;
  * PyPlzResolver — pure Python for the test suite and tools/build_db.py
                    without QGIS (GeoJSON only), considerably slower.
"""

import os
import re
import gzip
import json
import math
import sqlite3
from array import array
from datetime import datetime

try:
    from qgis.core import (
        QgsVectorLayer,
        QgsSpatialIndex,
        QgsGeometry,
        QgsPointXY,
        QgsRectangle,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
    )
    QGIS_AVAILABLE = True
except ImportError:          # test suite / tools/build_db.py without QGIS
    QGIS_AVAILABLE = False

PLUGIN_DIR   = os.path.dirname(os.path.abspath(__file__))
BUNDLED_GEO  = os.path.join(PLUGIN_DIR, 'data', 'plz_gebiete.geojson')
BUNDLED_META = os.path.join(PLUGIN_DIR, 'data', 'plz_gebiete.json')

_ENABLED  = True      # test suite switches off the automatic fill-in
_OVERRIDE = None      # custom PLZ file: dict(path, field, layer, name)
_CACHE    = {}        # source key -> loaded resolver
_NACHLAUF_CHUNK = 50000   # rows per chunk of the PLZ post-processing


def _n(x):
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


def attr_text(val):
    """Attribute value of a feature as text. NULL — None, QVariant NULL
    (QGIS 3 / PyQt5: str() would yield 'NULL') or qgis.core.NULL — becomes ''.
    Whole-number floats (12.0) lose the '.0'."""
    if val is None:
        return ''
    try:
        if hasattr(val, 'isNull') and val.isNull():
            return ''
    except Exception:
        pass
    if isinstance(val, float):
        if val != val:                      # NaN
            return ''
        if val.is_integer():
            return str(int(val))
    return str(val).strip()


def plz_text(val):
    """PLZ attribute as text; a numeric PLZ that lost its leading zero
    (1067, 1067.0 or '1067') becomes '01067'."""
    s = attr_text(val)
    if len(s) == 4 and s.isdigit():
        s = '0' + s
    return s


# ── Active source ────────────────────────────────────────────────────────────

def set_enabled(flag):
    global _ENABLED
    _ENABLED = bool(flag)


def set_override(cfg):
    """Set a custom PLZ file (dict with path, field, optional layer, name)
    or None for the bundled areas."""
    global _OVERRIDE
    _OVERRIDE = dict(cfg) if cfg and cfg.get('path') else None


def get_override():
    return dict(_OVERRIDE) if _OVERRIDE else None


def bundled_meta():
    try:
        with open(BUNDLED_META, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def active_source():
    """Description of the active PLZ source, or None (disabled/missing).
    'key' changes as soon as the data changes — the PLZ derived from areas
    are then recomputed in the post-processing step."""
    if not _ENABLED:
        return None
    if _OVERRIDE:
        p = _OVERRIDE['path']
        if not os.path.exists(p):
            return None
        mtimes = _source_mtimes(p)
        mtime = max(mtimes) if mtimes else int(os.path.getmtime(p))
        layer = _OVERRIDE.get('layer') or None
        field = _OVERRIDE.get('field') or 'plz'
        # The key must change with everything that changes the areas: path,
        # layer of a GeoPackage, field and the times of all relevant files
        # (.shp AND .dbf of a shapefile).
        return {'path': p, 'field': field,
                'layer': layer,
                'name': _OVERRIDE.get('name') or os.path.basename(p),
                'stand': datetime.fromtimestamp(mtime).date().isoformat(),
                'lizenz': _OVERRIDE.get('lizenz', ''), 'attribution': '',
                'eigene': True, 'crs': None,
                'key': (f'eigene:{os.path.abspath(p)}|{layer or ""}|{field}|'
                        + ':'.join(str(t) for t in mtimes))}
    if not os.path.exists(BUNDLED_GEO):
        return None
    m = bundled_meta()
    stand = m.get('stand', '')
    return {'path': BUNDLED_GEO, 'field': m.get('feld', 'plz'), 'layer': None,
            'name': f'PLZ-Gebiete (OpenStreetMap), Stand {_date_de(stand)}',
            'stand': stand, 'lizenz': m.get('lizenz', 'ODbL 1.0'),
            'attribution': m.get('attribution', '© OpenStreetMap-Mitwirkende'),
            'eigene': False, 'crs': m.get('crs', 'EPSG:25832'),
            # checksum as a CRC32 number (the plugin repository's security scan
            # treats a hex checksum as a potential secret)
            'key': f'osm:{stand}:{m.get("pruefsumme_crc32") or (m.get("sha256") or "")[:12]}'}


def _source_mtimes(path):
    """Modification times (int seconds) of a PLZ file and its relevant sidecar
    files: .shp and .dbf of a shapefile, the -wal file of a GeoPackage."""
    base, ext = os.path.splitext(path)
    ext = ext.lower()
    files = [path]
    if ext == '.shp':
        files += [base + e for e in ('.dbf', '.DBF') if os.path.exists(base + e)][:1]
    elif ext == '.gpkg' and os.path.exists(path + '-wal'):
        files.append(path + '-wal')
    out = []
    for f in files:
        try:
            out.append(int(os.path.getmtime(f)))
        except OSError:
            pass
    return out


def _date_de(iso):
    try:
        y, mo, d = iso[:10].split('-')
        return f'{d}.{mo}.{y}'
    except Exception:
        return iso or '?'


# ── Computation path with QGIS ───────────────────────────────────────────────

class PlzResolver:
    def __init__(self, source, plz_field="plz", layer_name=None, crs=None):
        """
        source     : path to .gpkg, .shp, .geojson or .geojson.gz
        plz_field  : name of the column holding the postcode
        layer_name : layer name for GeoPackage; otherwise None
        crs        : forced coordinate system (e.g. 'EPSG:25832') if the
                     file does not specify one
        """
        self.ok = False
        self.error = ""
        self.area_count = 0
        self._tcache = {}     # epsg -> QgsCoordinateTransform
        self._geoms = {}      # fid  -> QgsGeometry (keeps the geometry alive!)
        self._engines = {}    # fid  -> prepared geometry engine (lazy)
        self._plz = {}        # fid  -> PLZ value (str)

        if not QGIS_AVAILABLE:
            self.error = "PLZ-Ergänzung mit diesem Rechenweg benötigt QGIS."
            return
        path = source
        if path.lower().endswith('.gz'):
            path = '/vsigzip/' + path
        uri = path if not layer_name else "{}|layername={}".format(path, layer_name)
        layer = QgsVectorLayer(uri, "plz_lookup", "ogr")
        if not layer.isValid():
            self.error = "PLZ-Datei konnte nicht geladen werden: {}".format(source)
            return
        if crs:
            layer.setCrs(QgsCoordinateReferenceSystem(crs))

        field_names = [f.name() for f in layer.fields()]
        if plz_field not in field_names:
            self.error = ('Spalte "{}" nicht in der PLZ-Datei (vorhanden: {})'
                          .format(plz_field, ", ".join(field_names)))
            return

        plz_idx = layer.fields().indexFromName(plz_field)
        self.layer_crs = layer.crs()
        self._index = QgsSpatialIndex()
        self._transform_context = QgsProject.instance().transformContext()

        # Load index + geometries. The expensive prepareGeometry() happens lazily
        # per area on the first hit (_engine_for). The QgsGeometry objects must
        # stay referenced, otherwise the engine points to freed memory.
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            fid = feat.id()
            self._index.addFeature(feat)
            self._geoms[fid] = geom
            self._plz[fid] = plz_text(feat.attribute(plz_idx))
        del layer   # release in the same thread it was created in

        if not self._geoms:
            self.error = "PLZ-Datei enthält keine nutzbaren Flächen."
            return
        self.area_count = len(self._geoms)
        self.ok = True

    def _transform(self, epsg):
        t = self._tcache.get(epsg)
        if t is None:
            src = QgsCoordinateReferenceSystem.fromEpsgId(int(epsg))
            t = QgsCoordinateTransform(src, self.layer_crs, self._transform_context)
            self._tcache[epsg] = t
        return t

    def _engine_for(self, fid):
        engine = self._engines.get(fid)
        if engine is None:
            geom = self._geoms.get(fid)
            if geom is None:
                return None
            engine = QgsGeometry.createGeometryEngine(geom.constGet())
            engine.prepareGeometry()
            self._engines[fid] = engine
        return engine

    def lookup(self, x, y, epsg):
        """PLZ for the point (x, y) in the source EPSG, otherwise ''."""
        if not self.ok:
            return ""
        try:
            pt = self._transform(epsg).transform(QgsPointXY(float(x), float(y)))
        except Exception:
            return ""
        X, Y = pt.x(), pt.y()
        # The QgsGeometry MUST stay in a variable: constGet() only returns a
        # pointer into it — a temporary would be freed immediately and QGIS
        # crashes in contains().
        point_geom = QgsGeometry.fromPointXY(QgsPointXY(X, Y))
        const_pt = point_geom.constGet()
        for fid in self._index.intersects(QgsRectangle(X, Y, X, Y)):
            engine = self._engine_for(fid)
            if engine is not None and engine.contains(const_pt):
                return self._plz[fid]
        return ""


# ── Computation path without QGIS (pure Python) ──────────────────────────────

_A  = 6378137.0
_F  = 1 / 298.257222101            # GRS80 (ETRS89)
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996


def _utm_inverse(e, n, zone):
    """UTM (ETRS89) -> (lat, lon) in degrees (series expansion after Snyder)."""
    lon0 = math.radians(zone * 6 - 183)
    x = e - 500000.0
    m = n / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    p1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
          + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
          + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
          + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    s, c = math.sin(p1), math.cos(p1)
    n1 = _A / math.sqrt(1 - _E2 * s * s)
    t1 = math.tan(p1) ** 2
    c1 = _EP2 * c * c
    r1 = _A * (1 - _E2) / (1 - _E2 * s * s) ** 1.5
    d = x / (n1 * _K0)
    lat = p1 - (n1 * math.tan(p1) / r1) * (
        d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = lon0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1) * d ** 5 / 120) / c
    return math.degrees(lat), math.degrees(lon)


def _utm_forward(lat, lon, zone):
    """(lat, lon) in degrees -> UTM (ETRS89) (series expansion after Snyder)."""
    phi, lam = math.radians(lat), math.radians(lon)
    lam0 = math.radians(zone * 6 - 183)
    s, c = math.sin(phi), math.cos(phi)
    n = _A / math.sqrt(1 - _E2 * s * s)
    t = math.tan(phi) ** 2
    cc = _EP2 * c * c
    a = (lam - lam0) * c
    m = _A * ((1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256) * phi
              - (3 * _E2 / 8 + 3 * _E2 ** 2 / 32 + 45 * _E2 ** 3 / 1024) * math.sin(2 * phi)
              + (15 * _E2 ** 2 / 256 + 45 * _E2 ** 3 / 1024) * math.sin(4 * phi)
              - (35 * _E2 ** 3 / 3072) * math.sin(6 * phi))
    e = _K0 * n * (a + (1 - t + cc) * a ** 3 / 6
                   + (5 - 18 * t + t * t + 72 * cc - 58 * _EP2) * a ** 5 / 120) + 500000.0
    nn = _K0 * (m + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * cc + 4 * cc * cc) * a ** 4 / 24
                                         + (61 - 58 * t + t * t + 600 * cc - 330 * _EP2) * a ** 6 / 720))
    return e, nn


def _epsg_zone(epsg):
    epsg = int(epsg)
    if 25828 <= epsg <= 25838:
        return epsg - 25800
    raise ValueError(f'EPSG:{epsg} wird ohne QGIS nicht unterstützt')


class PyPlzResolver:
    """Point-in-polygon without QGIS for GeoJSON(.gz) in ETRS89/UTM. Grid index
    over the area bounding boxes, ray casting (even/odd across all rings, so
    that holes are handled correctly too)."""

    CELL = 5000.0

    def __init__(self, source, plz_field='plz', crs=None):
        self.ok = False
        self.error = ''
        self.area_count = 0
        try:
            opener = gzip.open if source.lower().endswith('.gz') else open
            with opener(source, 'rt', encoding='utf-8') as f:
                fc = json.load(f)
        except Exception as e:
            self.error = f'PLZ-Datei konnte nicht gelesen werden: {e}'
            return
        name = crs or ((fc.get('crs') or {}).get('properties') or {}).get('name', '')
        m = re.search(r'(\d{4,5})\s*$', name or '')
        if not m:
            self.error = 'PLZ-Datei ohne Koordinatensystem-Angabe (ohne QGIS nur ETRS89/UTM).'
            return
        try:
            self.zone = _epsg_zone(int(m.group(1)))
        except ValueError as e:
            self.error = str(e)
            return
        self._feats = []         # (plz, bbox, [(xs, ys), ...])
        self._grid = {}
        for feat in fc.get('features', []):
            plz = plz_text((feat.get('properties') or {}).get(plz_field))
            geom = feat.get('geometry') or {}
            polys = geom.get('coordinates') or []
            if geom.get('type') == 'Polygon':
                polys = [polys]
            elif geom.get('type') != 'MultiPolygon':
                continue
            rings = []
            x0 = y0 = float('inf')
            x1 = y1 = float('-inf')
            for poly in polys:
                for ring in poly:
                    xs = array('d', (p[0] for p in ring))
                    ys = array('d', (p[1] for p in ring))
                    if len(xs) < 3:
                        continue
                    rings.append((xs, ys))
                    x0, x1 = min(x0, min(xs)), max(x1, max(xs))
                    y0, y1 = min(y0, min(ys)), max(y1, max(ys))
            if not rings or not plz:
                continue
            idx = len(self._feats)
            self._feats.append((plz, (x0, y0, x1, y1), rings))
            for gx in range(int(x0 // self.CELL), int(x1 // self.CELL) + 1):
                for gy in range(int(y0 // self.CELL), int(y1 // self.CELL) + 1):
                    self._grid.setdefault((gx, gy), []).append(idx)
        self.area_count = len(self._feats)
        if not self._feats:
            self.error = 'PLZ-Datei enthält keine nutzbaren Flächen.'
            return
        self.ok = True

    def _to_layer(self, x, y, epsg):
        if int(epsg) in (4326, 4258):          # geographic (lon, lat)
            return _utm_forward(y, x, self.zone)
        z = _epsg_zone(epsg)
        if z == self.zone:
            return x, y
        lat, lon = _utm_inverse(x, y, z)
        return _utm_forward(lat, lon, self.zone)

    @staticmethod
    def _inside(x, y, rings):
        inside = False
        for xs, ys in rings:
            j = len(xs) - 1
            for i in range(len(xs)):
                yi, yj = ys[i], ys[j]
                if (yi > y) != (yj > y):
                    if x < (xs[j] - xs[i]) * (y - yi) / (yj - yi) + xs[i]:
                        inside = not inside
                j = i
        return inside

    def lookup(self, x, y, epsg):
        if not self.ok:
            return ''
        try:
            X, Y = self._to_layer(float(x), float(y), epsg)
        except (ValueError, TypeError):
            return ''
        for idx in self._grid.get((int(X // self.CELL), int(Y // self.CELL)), ()):
            plz, (x0, y0, x1, y1), rings = self._feats[idx]
            if x0 <= X <= x1 and y0 <= Y <= y1 and self._inside(X, Y, rings):
                return plz
        return ''


# ── Loading with cache, counting per use ─────────────────────────────────────

class ResolverSession:
    """Counts hits per import/post-processing run; the loaded resolver itself is
    reused between imports (loading takes a few seconds)."""

    def __init__(self, resolver, source):
        self._r = resolver
        self.source = source
        self.area_count = resolver.area_count
        self.matched = 0
        self.missed = 0

    def lookup(self, x, y, epsg):
        p = self._r.lookup(x, y, epsg)
        if p:
            self.matched += 1
        else:
            self.missed += 1
        return p

    def stats(self):
        return {'matched': self.matched, 'missed': self.missed}

    def info(self):
        """Data for plz_meta (log of the PLZ fill-in per Bundesland)."""
        return {'layer': self.source['name'], 'feld': self.source['field'],
                'ergaenzt': self.matched, 'ohne_flaeche': self.missed,
                'quelle_key': self.source['key']}


def make_resolver(source, plz_field="plz", layer_name=None, crs=None):
    """Resolver for a file — with QGIS, otherwise (GeoJSON only) in pure Python."""
    if not source:
        return None
    if QGIS_AVAILABLE:
        return PlzResolver(source, plz_field=plz_field, layer_name=layer_name, crs=crs)
    return PyPlzResolver(source, plz_field=plz_field, crs=crs)


def load_active(detail=None):
    """ResolverSession for the active source (cached), or None."""
    src = active_source()
    if not src:
        return None
    r = _CACHE.get(src['key'])
    if r is None:
        if detail:
            detail(f'Lade PLZ-Gebiete: {src["path"]}')
        r = make_resolver(src['path'], plz_field=src['field'], layer_name=src['layer'],
                          crs=src.get('crs'))
        if not r.ok:
            if detail:
                detail(f'PLZ-Gebiete nicht nutzbar: {r.error}')
            return None
        _CACHE.clear()
        _CACHE[src['key']] = r
        if detail:
            detail(f'{r.area_count} PLZ-Gebiete geladen ({type(r).__name__}).')
    return ResolverSession(r, src)


def build_resolver_logged(source=None, plz_field="plz", layer_name=None, log=None, detail=None):
    """Resolver for an import: always the active source (bundled postcode
    areas or the custom file set via set_override). source, plz_field and
    layer_name are ignored — they only keep older calls working.
    Returns a ResolverSession or None."""
    return load_active(detail)


# ── Post-processing: PLZ for already imported addresses ──────────────────────

def ergaenze_plz(db_path, bundeslaender=None, log=None, detail=None, progress=None,
                 cancel=None):
    """Fill in missing PLZ in the database from the postcode areas.

    Per Bundesland, addresses without PLZ are computed; if the postcode areas
    have changed since the last fill-in, also all PLZ previously derived from
    areas (plz_herkunft = 'gebiet'). PLZ from the source remain untouched.
    Returns {bundesland: number_filled_in}."""
    from . import local_geocoder as _lc
    _log = log or (lambda m: None)
    src = active_source()
    if not src or not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path, timeout=30)
    result = {}
    session = None
    try:
        _lc.ensure_plz_herkunft(conn)
        _lc.ensure_plz_meta(conn)
        if bundeslaender:
            states = list(bundeslaender)
        elif _lc._table_exists(conn, 'bundesland_index'):
            states = [r[0] for r in conn.execute(
                "SELECT bundesland FROM bundesland_index ORDER BY 1").fetchall()]
        else:
            states = [r[0] for r in conn.execute(
                "SELECT DISTINCT bundesland FROM addresses ORDER BY 1").fetchall()]
        meta = {r[0]: r[1] for r in conn.execute("SELECT bundesland, quelle_key FROM plz_meta")}
        for bl in states:
            if cancel and cancel():
                break
            # Recompute only if a fill-in was previously done with a DIFFERENT
            # version of the areas. Without an entry there can be no PLZ derived
            # from areas yet — then the (index-backed) gaps are sufficient.
            old_key = meta.get(bl)
            recompute = bool(old_key) and old_key != src['key']
            conds = ["plz = '' AND bundesland = ?", "plz IS NULL AND bundesland = ?"]
            if recompute:
                # first, so that the gaps filled below are not computed twice
                conds.insert(0, "bundesland = ? AND plz_herkunft = 'gebiet' AND plz <> ''")
            total = sum(conn.execute(f"SELECT COUNT(*) FROM addresses WHERE {c}",
                                     (bl,)).fetchone()[0] for c in conds)
            if not total:
                if old_key != src['key']:   # remember the version so it is not re-checked every time
                    _lc.record_plz_meta(conn, bl, {'layer': src['name'], 'feld': src['field'],
                                                   'ergaenzt': 0, 'ohne_flaeche': 0,
                                                   'quelle_key': src['key']}, True)
                    conn.commit()
                continue
            if session is None:
                session = load_active(detail)
                if session is None:
                    return result
            if detail:
                detail(f'{bl}: PLZ-Nachlauf für {total} Adressen '
                       f'({"Neuberechnung" if recompute else "nur fehlende"})')
            n_new, n_upd, done, cancelled = 0, 0, 0, False
            # Streamed in chunks (a whole state would need up to ~2 GB in QGIS);
            # only rows already read are updated, so the scan is not disturbed.
            for cond in conds:
                cur = conn.execute("SELECT id, x, y, epsg, plz, plz_herkunft FROM addresses "
                                   f"WHERE {cond}", (bl,))
                while True:
                    chunk = cur.fetchmany(_NACHLAUF_CHUNK)
                    if not chunk:
                        break
                    if cancel and cancel():
                        cancelled = True
                        break
                    updates, cleared = [], []
                    for aid, x, y, epsg, plz, herkunft in chunk:
                        p = session.lookup(x, y, epsg or 25832)
                        if p:
                            if p != (plz or '') or herkunft != 'gebiet':
                                updates.append((p, aid))
                                if not plz:
                                    n_new += 1
                        elif herkunft == 'gebiet':
                            cleared.append((aid,))
                    conn.executemany("UPDATE addresses SET plz = ?, plz_herkunft = 'gebiet' "
                                     "WHERE id = ?", updates)
                    conn.executemany("UPDATE addresses SET plz = '', plz_herkunft = NULL "
                                     "WHERE id = ?", cleared)
                    n_upd += len(updates)
                    done += len(chunk)
                    if progress:
                        progress(done, total, f'{bl}: PLZ werden ergänzt …')
                cur.close()
                if cancelled:
                    break
            if cancelled:
                # The PLZ computed so far are correct and kept, but the state is
                # NOT marked as done with the new version of the areas — the next
                # run continues (missing PLZ / recomputation with the old key).
                _lc.refresh_plz_coverage(conn, bl)
                conn.commit()
                if detail:
                    detail(f'{bl}: PLZ-Nachlauf abgebrochen ({done} von {total} Adressen).')
                break
            info = session.info()
            _lc.record_plz_meta(conn, bl, info, True)
            _lc.refresh_plz_coverage(conn, bl)
            conn.commit()
            session.matched = session.missed = 0
            result[bl] = n_new
            if n_new:
                _log(f'{bl}: PLZ für {_n(n_new)} Adressen ergänzt.')
            elif n_upd and recompute:
                _log(f'{bl}: PLZ aus den neuen PLZ-Gebieten aktualisiert.')
        return result
    finally:
        conn.close()
