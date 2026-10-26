# -*- coding: utf-8 -*-
"""Postcodes from postcode areas: pure Python computation, automatic filling on
import, follow-up run for existing gaps, recomputation with new areas,
precedence of the postcode from the source."""
import os
import gzip
import json
import sqlite3
import importlib

import pytest

from conftest import lg, pr, PKG_NAME
from test_source_downloader import _hkde_line

dbi = importlib.import_module(f'{PKG_NAME}.db_import_worker')


def _square(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


def _write_areas(path, areas, crs='urn:ogc:def:crs:EPSG::25832', gz=True):
    """areas: [(plz, polygon_rings | [polygons])]"""
    feats = []
    for plz, geom in areas:
        typ = 'MultiPolygon' if isinstance(geom[0][0][0], list) else 'Polygon'
        feats.append({'type': 'Feature', 'properties': {'plz': plz},
                      'geometry': {'type': typ, 'coordinates': geom}})
    fc = {'type': 'FeatureCollection', 'crs': {'type': 'name', 'properties': {'name': crs}},
          'features': feats}
    data = json.dumps(fc).encode()
    if gz:
        with gzip.open(path, 'wb') as f:
            f.write(data)
    else:
        open(path, 'wb').write(data)
    return path


@pytest.fixture
def areas(tmp_path, monkeypatch):
    """Two postcode areas around the Bochum test addresses (UTM 32), one with a hole."""
    path = _write_areas(str(tmp_path / 'plz.geojson.gz'), [
        ('44869', _square(370000, 5700000, 372000, 5702000)),
        ('44866', [[[372000, 5700000], [374000, 5700000], [374000, 5702000], [372000, 5702000], [372000, 5700000]],
                   [[372500, 5700500], [373500, 5700500], [373500, 5701500], [372500, 5701500], [372500, 5700500]]]),
    ])
    monkeypatch.setattr(pr, 'BUNDLED_GEO', path)
    meta = str(tmp_path / 'plz.json')
    json.dump({'stand': '2026-09-15', 'sha256': 'a' * 64, 'crs': 'EPSG:25832', 'feld': 'plz',
               'lizenz': 'ODbL 1.0', 'attribution': '© OpenStreetMap-Mitwirkende'}, open(meta, 'w'))
    monkeypatch.setattr(pr, 'BUNDLED_META', meta)
    pr._CACHE.clear()
    pr.set_override(None)
    pr.set_enabled(True)
    yield path
    pr.set_enabled(False)
    pr._CACHE.clear()


# ── Computation without QGIS ─────────────────────────────────────────────────

def test_utm_roundtrip_and_zone_change():
    for lat, lon in ((52.5163, 13.3777), (51.05, 6.9), (54.3, 10.1), (47.6, 9.5)):
        for zone in (32, 33):
            e, n = pr._utm_forward(lat, lon, zone)
            la, lo = pr._utm_inverse(e, n, zone)
            # < 1 m even far from the central meridian (Görlitz in UTM 32: 6°); postcode areas are generalized to 5 m
            assert abs(la - lat) < 1e-5 and abs(lo - lon) < 1e-5
    # Berlin (UTM 33) → UTM 32: about 4.4° from the central meridian, round trip < 1 cm
    e33, n33 = pr._utm_forward(52.5163, 13.3777, 33)
    lat, lon = pr._utm_inverse(e33, n33, 33)
    e32, n32 = pr._utm_forward(lat, lon, 32)
    assert 790000 < e32 < 800000 and 5825000 < n32 < 5835000


def test_py_resolver_polygon_hole_and_outside(areas):
    r = pr.PyPlzResolver(areas)
    assert r.ok and r.area_count == 2
    assert r.lookup(371000, 5701000, 25832) == '44869'
    assert r.lookup(372200, 5700200, 25832) == '44866'
    assert r.lookup(373000, 5701000, 25832) == ''            # in the hole
    assert r.lookup(380000, 5701000, 25832) == ''            # outside
    lat, lon = pr._utm_inverse(371000, 5701000, 32)
    assert r.lookup(lon, lat, 4326) == '44869'                # geographic (lon, lat)
    assert r.lookup(lon, lat, 4258) == '44869'
    assert r.lookup(371000, 5701000, 3857) == ''              # not supported → empty


def test_py_resolver_needs_crs(tmp_path):
    p = _write_areas(str(tmp_path / 'x.geojson'), [('1', _square(0, 0, 1, 1))], crs='', gz=False)
    r = pr.PyPlzResolver(p)
    assert not r.ok and 'Koordinatensystem' in r.error


def test_active_source_and_override(areas, tmp_path):
    src = pr.active_source()
    assert src['eigene'] is False and src['key'].startswith('osm:2026-09-15:')
    assert 'Stand 15.09.2026' in src['name']
    own = _write_areas(str(tmp_path / 'post.geojson'), [('44869', _square(0, 0, 1, 1))], gz=False)
    pr.set_override({'path': own, 'field': 'plz', 'name': 'Post-PLZ'})
    o = pr.active_source()
    assert o['eigene'] and o['name'] == 'Post-PLZ' and o['key'] != src['key']
    pr.set_override(None)
    pr.set_enabled(False)
    assert pr.active_source() is None


# ── Import fills in automatically ────────────────────────────────────────────

def _hkde_file(path, rows):
    open(path, 'w', encoding='utf-8').write('\n'.join(
        _hkde_line('Bochum', s, hn, x, y) for s, hn, x, y in rows) + '\n')
    return path


def test_import_fills_missing_plz_and_marks_origin(db, areas, tmp_path):
    f = _hkde_file(str(tmp_path / 'nrw.txt'), [('Weg A', '1', 371000.0, 5701000.0),
                                               ('Weg B', '2', 372200.0, 5700200.0),
                                               ('Weg C', '3', 390000.0, 5701000.0)])
    w = dbi.DbImportWorker(f, 'Nordrhein-Westfalen', replace=True)
    result, logs = {}, []
    w.log.connect(logs.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    conn = db.get_connection()
    rows = conn.execute("SELECT strasse, plz, plz_herkunft FROM addresses "
                        "WHERE bundesland='Nordrhein-Westfalen' ORDER BY strasse").fetchall()
    meta = conn.execute("SELECT layer, ergaenzt, ohne_flaeche, quelle_key FROM plz_meta "
                        "WHERE bundesland='Nordrhein-Westfalen'").fetchone()
    conn.close()
    assert [tuple(r) for r in rows] == [('Weg A', '44869', 'gebiet'), ('Weg B', '44866', 'gebiet'),
                                       ('Weg C', '', None)]
    assert meta[1:3] == (2, 1) and meta[3].startswith('osm:') and 'PLZ-Gebiete' in meta[0]
    assert any('PLZ ergänzt für 2 Adressen' in m for m in logs)


# ── Follow-up run ────────────────────────────────────────────────────────────

def test_nachlauf_fills_gaps_keeps_source_plz_and_recomputes(db, areas, monkeypatch, tmp_path):
    conn = sqlite3.connect(db.DB_PATH)
    lg.ensure_plz_herkunft(conn)
    conn.execute("DELETE FROM addresses WHERE bundesland='Nordrhein-Westfalen'")
    conn.executemany(
        "INSERT INTO addresses (bundesland, gemeinde, plz, strasse, hausnummer, epsg, x, y, "
        "strasse_norm, gemeinde_norm, plz_herkunft) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [('Nordrhein-Westfalen', 'Bochum', '', 'A', '1', 25832, 371000, 5701000, 'a', 'bochum', None),
         ('Nordrhein-Westfalen', 'Bochum', None, 'B', '2', 25832, 372200, 5700200, 'b', 'bochum', None),
         ('Nordrhein-Westfalen', 'Bochum', '44000', 'C', '3', 25832, 371000, 5701000, 'c', 'bochum', None)])
    db.rebuild_lookup_tables(conn)
    conn.commit()
    conn.close()
    assert db.get_plz_coverage()['Nordrhein-Westfalen'] == (3, 2)

    logs = []
    res = pr.ergaenze_plz(db.DB_PATH, log=logs.append)
    assert res.get('Nordrhein-Westfalen') == 2
    conn = sqlite3.connect(db.DB_PATH)
    got = dict(conn.execute("SELECT strasse, plz || '/' || coalesce(plz_herkunft,'') FROM addresses "
                            "WHERE bundesland='Nordrhein-Westfalen'").fetchall())
    conn.close()
    assert got == {'A': '44869/gebiet', 'B': '44866/gebiet', 'C': '44000/'}    # source is kept
    assert db.get_plz_coverage()['Nordrhein-Westfalen'] == (3, 0)
    assert logs == ['Nordrhein-Westfalen: PLZ für 2 Adressen ergänzt.']

    # second run: nothing to do (same version)
    assert pr.ergaenze_plz(db.DB_PATH, bundeslaender=['Nordrhein-Westfalen']) == {}

    # new version of the postcode areas: A now lies in 44870 → is recomputed,
    # C (postcode from the source) is kept
    new = _write_areas(str(tmp_path / 'neu.geojson.gz'), [
        ('44870', _square(370000, 5700000, 372000, 5702000)),
        ('44866', _square(372000, 5700000, 374000, 5702000))])
    monkeypatch.setattr(pr, 'BUNDLED_GEO', new)
    meta = json.load(open(pr.BUNDLED_META))
    meta['stand'] = '2027-09-15'
    json.dump(meta, open(pr.BUNDLED_META, 'w'))
    pr.ergaenze_plz(db.DB_PATH, bundeslaender=['Nordrhein-Westfalen'])
    conn = sqlite3.connect(db.DB_PATH)
    got = dict(conn.execute("SELECT strasse, plz FROM addresses WHERE bundesland='Nordrhein-Westfalen'"))
    key = conn.execute("SELECT quelle_key FROM plz_meta WHERE bundesland='Nordrhein-Westfalen'").fetchone()[0]
    conn.close()
    assert got == {'A': '44870', 'B': '44866', 'C': '44000'} and key.startswith('osm:2027-09-15')


def test_nachlauf_without_source_is_noop(db):
    pr.set_enabled(False)
    assert pr.ergaenze_plz(db.DB_PATH) == {}


def test_init_db_adds_column_to_old_database(tmp_path):
    p = str(tmp_path / 'alt.db')
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE addresses (id INTEGER PRIMARY KEY, bundesland TEXT NOT NULL, kreis TEXT, "
                 "gemeinde TEXT, plz TEXT, strasse TEXT NOT NULL, hausnummer TEXT, hausnr_zusatz TEXT, "
                 "epsg INTEGER, x REAL NOT NULL, y REAL NOT NULL, strasse_norm TEXT, gemeinde_norm TEXT)")
    lg.init_db(conn)
    assert 'plz_herkunft' in lg._columns(conn, 'addresses')
    conn.close()


def test_bundled_data_file_is_consistent():
    """The bundled file matches its metadata (without loading it completely)."""
    if not os.path.exists(os.path.join(pr.PLUGIN_DIR, 'data', 'plz_gebiete.geojson')):
        pytest.skip('mitgelieferte PLZ-Gebiete fehlen')
    meta = json.load(open(os.path.join(pr.PLUGIN_DIR, 'data', 'plz_gebiete.json'), encoding='utf-8'))
    assert 8000 <= meta['anzahl'] <= 8400 and meta['crs'] == 'EPSG:25832' and meta['lizenz'] == 'ODbL 1.0'
    # Source is OpenStreetMap itself — no detour via a commercial provider
    assert 'openstreetmap' in meta['quelle_url'] and 'esri' not in json.dumps(meta).lower()
    # readable GeoJSON (no compressed files in the repository)
    assert not os.path.exists(os.path.join(pr.PLUGIN_DIR, 'data', 'plz_gebiete.geojson.gz'))
    with open(os.path.join(pr.PLUGIN_DIR, 'data', 'plz_gebiete.geojson'), 'rb') as f:
        head = f.read(200).decode('utf-8')
    assert '"urn:ogc:def:crs:EPSG::25832"' in head
