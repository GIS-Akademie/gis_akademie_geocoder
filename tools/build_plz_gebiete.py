# -*- coding: utf-8 -*-
"""
Generates the plugin's bundled postcode areas directly from OpenStreetMap:

    data/plz_gebiete.geojson      — postcode polygons, ETRS89/UTM 32 (EPSG:25832),
                                    generalized to 5 m, coordinates rounded to 1 m
                                    (readable GeoJSON — no compressed files in the
                                    repository; the plugin zip compresses them)
    data/plz_gebiete.json         — version date, source, license, count, checksum

Source: the postcode area boundaries in OpenStreetMap (relations
boundary=postal_code with postal_code=xxxxx) from the Germany extract by
Geofabrik (https://download.geofabrik.de/europe/germany.html, about 5 GB).
© OpenStreetMap contributors, license ODbL 1.0.

Reading uses GDAL's OSM driver — i.e. the Python environment of QGIS
(OSGeo4W: python-qgis.bat) or a Python with GDAL. Important: the driver's
node index must stay in memory (MAX_TMPFILE_SIZE, default here 10 GB,
--speicher) — with GDAL's default of 100 MB the driver reads the index from
disk hundreds of times for all of Germany (hours).
The Overpass API is not a reliable alternative for this amount of data
(public servers reject the query under load).

Usage:
    python-qgis.bat tools/build_plz_gebiete.py                 # download extract and regenerate
    python-qgis.bat tools/build_plz_gebiete.py --osm de.osm.pbf  # use an existing file

After a new version: bump the plugin version — on the next
"Alle eingetragenen Quellen aktualisieren" (update all registered sources) the
postcodes derived from areas are recomputed automatically in the database.
"""
import os
import re
import sys
import json
import time
import shutil
import zlib
import argparse
import tempfile
import urllib.request
from datetime import datetime, timezone
import urllib.parse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(PLUGIN_DIR, 'data')
OUT_GEO    = os.path.join(DATA_DIR, 'plz_gebiete.geojson')
OUT_META   = os.path.join(DATA_DIR, 'plz_gebiete.json')

PBF_URL  = 'https://download.geofabrik.de/europe/germany-latest.osm.pbf'
INFO_URL = 'https://download.geofabrik.de/europe/germany.html'
UA       = {'User-Agent': 'QGIS-GISAkademieGeocoder-Build (+https://gis-akademie.de)'}
MIN_ANZAHL, MAX_ANZAHL = 8000, 8400       # Germany has about 8,200 postcode areas
_PLZ_RE  = re.compile(r'^\d{5}$')


def _download(dest):
    """Download the Germany extract; returns the data date (Last-Modified)."""
    from email.utils import parsedate_to_datetime
    req = urllib.request.Request(PBF_URL, headers=UA)
    t0, done, step = time.time(), 0, 0
    with urllib.request.urlopen(req, timeout=120) as r, open(dest + '.part', 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        lm = r.headers.get('Last-Modified')
        while True:
            chunk = r.read(1 << 22)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done * 10 // total > step:
                step = done * 10 // total
                print(f'  {done / 1e9:.1f} von {total / 1e9:.1f} GB')
    os.replace(dest + '.part', dest)
    print(f'  geladen in {time.time() - t0:.0f} s')
    return parsedate_to_datetime(lm).date().isoformat() if lm else None


def _config(path):
    """GDAL's osmconf.ini with postal_code as a separate column of the polygons."""
    from osgeo import gdal
    src = gdal.FindFile('gdal', 'osmconf.ini')
    out, section = [], None
    for ln in open(src, encoding='utf-8').read().splitlines():
        s = ln.strip()
        if s.startswith('[') and s.endswith(']'):
            section = s[1:-1]
        if section == 'multipolygons' and s.startswith('attributes='):
            ln = s + ',postal_code'
        out.append(ln)
    open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')


def read_postal_areas(pbf, toleranz, workdir, speicher_mb=10000):
    """{plz: ogr geometry (EPSG:25832, generalized)} from the OSM relations
    boundary=postal_code. Several relations of the same postcode are merged
    into one multipolygon."""
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    cfg = os.path.join(workdir, 'osmconf_plz.ini')
    _config(cfg)
    gdal.SetConfigOption('CPL_TMPDIR', workdir)
    ds = gdal.OpenEx(pbf, gdal.OF_VECTOR, allowed_drivers=['OSM'],
                     open_options=[f'CONFIG_FILE={cfg}', f'MAX_TMPFILE_SIZE={int(speicher_mb)}',
                                   'COMPRESS_NODES=YES'])
    src, dst = osr.SpatialReference(), osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst.ImportFromEPSG(25832)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)

    # Pre-filter in GDAL: only pass postcode relations to Python; all other
    # objects (millions of points, streets, buildings) are discarded by the driver itself
    for k in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(k)
        lyr.SetAttributeFilter("boundary = 'postal_code'" if lyr.GetName() == 'multipolygons'
                               else 'osm_id IS NULL')
    areas, skipped, t0, n = {}, 0, time.time(), 0
    i_b = i_p = None
    state = {'last': -1}

    def _progress(pct, _msg, _data):
        # Called by the driver also for pre-filtered (not delivered) objects
        p = int(pct * 50)
        if p != state['last']:
            state['last'] = p
            print(f'  {p * 2} % gelesen ({time.time() - t0:.0f} s)')
        return 1

    while True:
        feat, lyr, pct = ds.GetNextFeature(include_layer=True, include_pct=True,
                                           callback=_progress)
        if feat is None:
            break
        n += 1
        if lyr.GetName() != 'multipolygons':
            continue
        if i_b is None:
            d = lyr.GetLayerDefn()
            i_b, i_p = d.GetFieldIndex('boundary'), d.GetFieldIndex('postal_code')
        if feat.GetFieldAsString(i_b) != 'postal_code':
            continue
        plz = feat.GetFieldAsString(i_p).strip()
        g = feat.GetGeometryRef()
        if not _PLZ_RE.match(plz) or g is None or g.IsEmpty():
            skipped += 1
            continue
        g = g.Clone()
        g.Transform(tr)
        g = g.SimplifyPreserveTopology(toleranz) if toleranz else g
        if g is None or g.IsEmpty():
            skipped += 1
            continue
        if plz in areas:
            multi = ogr.ForceToMultiPolygon(areas[plz])
            for k in range(ogr.ForceToMultiPolygon(g).GetGeometryCount()):
                multi.AddGeometry(ogr.ForceToMultiPolygon(g).GetGeometryRef(k))
            areas[plz] = multi
        else:
            areas[plz] = g
    ds = None
    print(f'  fertig gelesen in {time.time() - t0:.0f} s: {len(areas)} PLZ-Gebiete'
          + (f', {skipped} Relationen ohne gültige PLZ/Geometrie übergangen' if skipped else ''))
    return areas


def _round_ring(ring):
    """Round coordinates to whole meters and remove the resulting duplicate
    points; rings with fewer than 4 points are dropped."""
    out = []
    for x, y in ((round(p[0]), round(p[1])) for p in ring):
        if not out or out[-1] != [x, y]:
            out.append([x, y])
    if out and out[0] != out[-1]:
        out.append(list(out[0]))
    return out if len(out) >= 4 else None


def round_geometry(geom):
    """Round a GeoJSON Polygon/MultiPolygon to 1 m (generalization is 5 m)."""
    polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
    neu = []
    for poly in polys:
        rings = [r for r in (_round_ring(r) for r in poly) if r]
        if rings and len(poly[0]) and _round_ring(poly[0]):
            neu.append(rings)
    if len(neu) == 1:
        return {'type': 'Polygon', 'coordinates': neu[0]}
    return {'type': 'MultiPolygon', 'coordinates': neu}


def main():
    ap = argparse.ArgumentParser(description='Mitgelieferte PLZ-Gebiete aus OpenStreetMap erzeugen')
    ap.add_argument('--osm', help='vorhandener Deutschland-Extrakt (.osm.pbf) statt Download')
    ap.add_argument('--speicher', type=int, default=10000,
                    help='Arbeitsspeicher für den Knotenindex in MB (Standard 10000)')
    ap.add_argument('--stand', help='Datenstand JJJJ-MM-TT (Standard: Zeitstempel der Daten)')
    ap.add_argument('--toleranz', type=float, default=5.0, help='Generalisierung in Metern (Standard 5)')
    args = ap.parse_args()
    try:
        from osgeo import ogr
        if ogr.GetDriverByName('OSM') is None:
            raise ImportError
    except ImportError:
        print('FEHLER: GDAL mit OSM-Treiber fehlt — bitte mit der Python-Umgebung von QGIS '
              'starten (OSGeo4W: python-qgis.bat).')
        return 1

    workdir = tempfile.mkdtemp(prefix='plz_osm_')
    try:
        if args.osm:
            src = args.osm
        else:
            src = os.path.join(workdir, 'germany-latest.osm.pbf')
            print('Lade den Deutschland-Extrakt von Geofabrik (rund 5 GB) …')
            args.stand = args.stand or _download(src)
        stand = args.stand or datetime.fromtimestamp(os.path.getmtime(src)).date().isoformat()
        print(f'Lese PLZ-Grenzen aus {os.path.basename(src)} (Stand {stand}, '
              f'Generalisierung {args.toleranz:g} m) …')
        areas = read_postal_areas(src, args.toleranz, workdir, args.speicher)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not MIN_ANZAHL <= len(areas) <= MAX_ANZAHL:
        print(f'FEHLER: {len(areas)} PLZ-Gebiete, erwartet {MIN_ANZAHL}–{MAX_ANZAHL}. '
              'Ist es der Deutschland-Extrakt?')
        return 1

    feats = [{'type': 'Feature', 'properties': {'plz': plz},
              'geometry': round_geometry(json.loads(g.ExportToJson()))}
             for plz, g in sorted(areas.items())]
    fc = {'type': 'FeatureCollection',
          # Without a crs entry QGIS/OGR read GeoJSON as WGS84 — but the
          # coordinates are UTM 32. Hence state it explicitly.
          'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::25832'}},
          'features': feats}
    raw = json.dumps(fc, separators=(',', ':')).encode('utf-8')
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_GEO, 'wb') as f:
        f.write(raw)
    alt_gz = OUT_GEO + '.gz'                     # remove the former compressed version
    if os.path.exists(alt_gz):
        os.remove(alt_gz)
    meta = {
        'name': 'PLZ-Gebiete Deutschland',
        'quelle': 'OpenStreetMap – Grenzen der Postleitzahlgebiete',
        'quelle_url': 'https://www.openstreetmap.org/',
        'auszug': 'Deutschland-Extrakt von Geofabrik',
        'auszug_url': INFO_URL,
        'rohdaten': 'OpenStreetMap (direkt)',
        'attribution': '© OpenStreetMap-Mitwirkende',
        'lizenz': 'ODbL 1.0',
        'lizenz_url': 'https://opendatacommons.org/licenses/odbl/1-0/',
        'stand': stand,
        'abgerufen': datetime.now(timezone.utc).date().isoformat(),
        'anzahl': len(feats),
        'crs': 'EPSG:25832',
        'generalisierung_m': args.toleranz,
        'feld': 'plz',
        # CRC32 instead of SHA-256: sufficient to detect new data; a long hex number
        # would be taken for a secret by the plugin repository's security scan
        'pruefsumme_crc32': zlib.crc32(raw),
    }
    with open(OUT_META, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'Fertig: {OUT_GEO} ({os.path.getsize(OUT_GEO) / 1e6:.1f} MB, '
          f'{len(raw) / 1e6:.0f} MB entpackt), {len(feats)} PLZ-Gebiete, Stand {stand}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
