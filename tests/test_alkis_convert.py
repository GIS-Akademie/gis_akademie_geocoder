# -*- coding: utf-8 -*-
"""ALKIS GeoPackage per district (Niedersachsen) -> address rows -> import.

Needs GDAL (included in QGIS); if it is missing, the tests are skipped.
Test data: a small GeoPackage with two municipalities (parcels) and buildings
with addresses — two of them in the grid cell on the municipal boundary."""
import os
import json
import zipfile
import importlib

import pytest

from conftest import PKG_NAME
ac  = importlib.import_module(f'{PKG_NAME}.alkis_convert')
oc  = importlib.import_module(f'{PKG_NAME}.osm_convert')
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')
from test_source_downloader import FakeHttp   # noqa: E402

needs_gdal = pytest.mark.skipif(not oc.gdal_available(), reason='GDAL fehlt')


def test_gemeinde_and_kreis_names():
    assert ac.gemeinde_name('Emden, Stadt') == 'Emden'
    assert ac.gemeinde_name('Hannover, Landeshauptstadt') == 'Hannover'
    assert ac.gemeinde_name('Garrel') == 'Garrel'
    assert ac.kreis_aus_dateiname('lkr_03453_Cloppenburg_kon.gpkg.zip') == 'Cloppenburg'
    assert ac.kreis_aus_dateiname('https://x/lkr_03402_Stadt_Emden_kon.gpkg.zip') == 'Stadt Emden'


def _square(ogr, x0, y0, x1, y1):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def _make_gpkg(path):
    from osgeo import ogr
    ds = ogr.GetDriverByName('GPKG').CreateDataSource(path)
    fl = ds.CreateLayer('flurstuecke', None, ogr.wkbPolygon)   # without coordinate system, like LGLN's data
    fl.CreateField(ogr.FieldDefn('gem__bez', ogr.OFTString))
    # Boundary at x = 401100: the grid cell 401000–401250 contains both municipalities
    for name, x0, x1 in (('Westdorf, Stadt', 400000, 401100), ('Ostdorf', 401100, 402000)):
        f = ogr.Feature(fl.GetLayerDefn())
        f.SetField('gem__bez', name)
        f.SetGeometry(_square(ogr, x0, 5800000, x1, 5801000))
        fl.CreateFeature(f)
    gb = ds.CreateLayer('gebaeude', None, ogr.wkbPolygon)
    for fld in ('bez', 'hnr', 'gfk'):
        gb.CreateField(ogr.FieldDefn(fld, ogr.OFTString))
    for bez, hnr, x in (('Hauptstraße', '1', 400100),
                        ('Hauptstraße', '2a', 401050),    # boundary cell, Westdorf
                        ('Feldweg', '3;5', 401150),       # boundary cell, Ostdorf
                        ('Feldweg', '7', 401800),
                        (None, '9', 401500),              # no street
                        ('Ohne', None, 401600)):          # no house number
        f = ogr.Feature(gb.GetLayerDefn())
        if bez:
            f.SetField('bez', bez)
        if hnr:
            f.SetField('hnr', hnr)
        f.SetGeometry(_square(ogr, x - 5, 5800500, x + 5, 5800510))
        gb.CreateFeature(f)
    ds = None


@pytest.fixture
def alkis_zip(tmp_path):
    gpkg = str(tmp_path / 'lkr_03999_Testkreis_kon.gpkg')
    _make_gpkg(gpkg)
    z = str(tmp_path / 'lkr_03999_Testkreis_kon.gpkg.zip')
    with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(gpkg, os.path.basename(gpkg))
    os.remove(gpkg)
    return z


@needs_gdal
def test_alkis_to_rows(alkis_zip, tmp_path):
    out = tmp_path / 'hk.txt'
    with open(out, 'w', encoding='utf-8', newline='') as fh:
        ac.write_header(fh)
        st = ac.alkis_to_rows(alkis_zip, fh, 'Niedersachsen', 'Testkreis')
    rows = [line.split(';') for line in out.read_text(encoding='utf-8').splitlines()[1:]]
    got = sorted((r[14], r[15], r[16], r[10]) for r in rows)
    assert got == [('Feldweg', '3', '', 'Ostdorf'), ('Feldweg', '5', '', 'Ostdorf'),
                   ('Feldweg', '7', '', 'Ostdorf'), ('Hauptstraße', '1', '', 'Westdorf'),
                   ('Hauptstraße', '2', 'a', 'Westdorf')]
    assert all(len(r) == 24 and r[4] == 'Niedersachsen' and r[3] == '03' and r[8] == 'Testkreis'
               for r in rows)
    assert st['adressen'] == 5 and st['ohne_gemeinde'] == 0


@needs_gdal
def test_worker_loads_index_geojson(db, monkeypatch, alkis_zip):
    idx_url = 'https://x/index.geojson'
    zip_url = 'https://x/lkr_03999_Testkreis_kon.gpkg.zip'
    body = open(alkis_zip, 'rb').read()
    index = json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'zip': zip_url}, 'geometry': None}]}).encode()

    def _zip(h):
        if h.get('If-None-Match') == '"z1"':
            return 304, {}, b''
        return 200, {'etag': '"z1"', 'last-modified': 'Fri, 23 Oct 2026 05:05:14 GMT'}, body
    http = FakeHttp({idx_url: index, zip_url: _zip})
    cat = {'version': 1, 'sources': {'Niedersachsen': {
        'typ': 'index_geojson', 'url': idx_url, 'url_feld': 'zip', 'format': 'alkis',
        'quelle_name': 'LGLN', 'lizenz': 'CC BY 4.0'}}}
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: http)

    w = suw.SourceUpdateWorker('Niedersachsen', plugin_version='t')
    logs, result = [], {}
    w.log.connect(logs.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    assert 'Testkreis: 5 Adressen' in logs
    conn = db.get_connection()
    n, gem = conn.execute("SELECT COUNT(*), GROUP_CONCAT(DISTINCT gemeinde) FROM addresses "
                          "WHERE bundesland='Niedersachsen'").fetchone()
    conn.close()
    assert n == 5 and set(gem.split(',')) == {'Westdorf', 'Ostdorf'}
    meta = db.get_source_meta('Niedersachsen')
    assert json.loads(meta['etag']) == {zip_url: '"z1"'} and meta['adressen'] == 5
    cache = sd.cache_dir_for(db.DB_PATH, 'Niedersachsen')
    assert not os.path.exists(cache) or not any(f.endswith('.zip') for f in os.listdir(cache))

    # Second run: all files unchanged (304) → no download, no import
    w2 = suw.SourceUpdateWorker('Niedersachsen', plugin_version='t')
    r2 = {}
    w2.finished.connect(lambda ok, msg: r2.update(ok=ok, msg=msg))
    w2.run()
    assert r2['ok'] and r2['msg'] == 'Keine neuen Daten seit dem letzten Import.'


def test_niedersachsen_catalog_entry():
    e = sd.load_catalog(include_user=False)['sources']['Niedersachsen']
    assert (e['typ'], e['format'], e['url_feld']) == ('index_geojson', 'alkis', 'zip')
    assert 'CC BY 4.0' in e['lizenz'] and e['min_abstand_tage'] == 28
    assert 'Niedersachsen' in sd.automatic_bundeslaender()


def test_index_geojson_urls_encodes_umlauts():
    idx = json.dumps({'features': [
        {'properties': {'zip': 'https://x/lkr_03158_Wolfenbüttel_kon.gpkg.zip'}},
        {'properties': {'zip': 'kein-link'}}]}).encode()
    urls = sd.index_geojson_urls({'url': 'https://x/i', 'url_feld': 'zip'},
                                 FakeHttp({'https://x/i': idx}))
    assert urls == ['https://x/lkr_03158_Wolfenb%C3%BCttel_kon.gpkg.zip']
