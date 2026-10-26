# -*- coding: utf-8 -*-
"""OpenStreetMap extract -> HK-BY file -> import (Bayern source).

The conversion needs GDAL with the OSM driver (included in QGIS); if it is
missing, these tests are skipped. A small OSM XML file serves as test data
(the OSM driver reads .osm just like .osm.pbf)."""
import os
import importlib

import pytest

from conftest import PKG_NAME
oc  = importlib.import_module(f'{PKG_NAME}.osm_convert')
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')

needs_gdal = pytest.mark.skipif(not oc.gdal_available(), reason='GDAL mit OSM-Treiber fehlt')


def test_split_housenumbers():
    assert oc.split_housenumbers('12') == [('12', '')]
    assert oc.split_housenumbers('12a') == [('12', 'a')]
    assert oc.split_housenumbers('12 B') == [('12', 'B')]
    assert oc.split_housenumbers('12;14') == [('12', ''), ('14', '')]
    assert oc.split_housenumbers('3, 5a') == [('3', ''), ('5', 'a')]
    assert oc.split_housenumbers('12-14') == [('12', '')]
    assert oc.split_housenumbers('3 1/2') == [('3', '1/2')]
    assert oc.split_housenumbers('ohne') == []
    assert oc.split_housenumbers('') == []


def test_bayern_catalog_entry_is_osm():
    e = sd.load_catalog(include_user=False)['sources']['Bayern']
    assert (e['typ'], e['format']) == ('direct', 'osm')
    assert e['url'] == 'https://download.geofabrik.de/europe/germany/bayern-latest.osm.pbf'
    assert e['min_abstand_tage'] == 28 and 'ODbL' in e['lizenz']
    assert 'Bayern' in sd.automatic_bundeslaender()


def test_min_abstand_applies_to_direct_sources(tmp_path):
    from datetime import datetime, timedelta, timezone
    cat = {'sources': {'Bayern': {'typ': 'direct', 'url': 'https://x/b.osm.pbf', 'format': 'osm',
                                  'min_abstand_tage': 28}}}
    prev = {'heruntergeladen_am': (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
    r = sd.update_source('Bayern', str(tmp_path / 'g.db'), prev_meta=prev, catalog=cat, http=object())
    assert r['status'] == 'skipped' and 'vor 3 Tagen' in r['text'] and '28 Tage' in r['text']


# ── Conversion with GDAL ─────────────────────────────────────────────────────
# Municipality "Testdorf" (square), containing: address point with city, address
# point without city (municipality from the boundary), house number list, building
# with the same address as a point (duplicate), point without street (ignored).
_OSM = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="48.10" lon="11.50"/>
  <node id="2" lat="48.10" lon="11.60"/>
  <node id="3" lat="48.20" lon="11.60"/>
  <node id="4" lat="48.20" lon="11.50"/>
  <node id="10" lat="48.150" lon="11.550">
    <tag k="addr:street" v="Dorfstraße"/><tag k="addr:housenumber" v="1"/>
    <tag k="addr:postcode" v="85748"/><tag k="addr:city" v="Testdorf"/>
    <tag k="addr:suburb" v="Oberdorf"/>
  </node>
  <node id="11" lat="48.151" lon="11.551">
    <tag k="addr:street" v="Dorfstraße"/><tag k="addr:housenumber" v="2a"/>
    <tag k="addr:postcode" v="85748"/>
  </node>
  <node id="12" lat="48.152" lon="11.552">
    <tag k="addr:street" v="Kirchweg"/><tag k="addr:housenumber" v="5;7"/>
    <tag k="addr:city" v="Testdorf"/>
  </node>
  <node id="13" lat="48.153" lon="11.553">
    <tag k="addr:housenumber" v="9"/>
  </node>
  <node id="20" lat="48.1500" lon="11.5500"/>
  <node id="21" lat="48.1500" lon="11.5503"/>
  <node id="22" lat="48.1502" lon="11.5503"/>
  <node id="23" lat="48.1502" lon="11.5500"/>
  <way id="100">
    <nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="20"/>
    <tag k="building" v="house"/>
    <tag k="addr:street" v="Dorfstraße"/><tag k="addr:housenumber" v="1"/>
    <tag k="addr:postcode" v="85748"/><tag k="addr:city" v="Testdorf"/>
  </way>
  <way id="200">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
  </way>
  <relation id="300">
    <member type="way" ref="200" role="outer"/>
    <tag k="type" v="boundary"/><tag k="boundary" v="administrative"/>
    <tag k="admin_level" v="8"/><tag k="name" v="Testdorf"/>
  </relation>
</osm>
"""


@pytest.fixture
def osm_file(tmp_path):
    p = tmp_path / 'test.osm'
    p.write_text(_OSM, encoding='utf-8')
    return str(p)


@needs_gdal
def test_osm_to_hkby(osm_file, tmp_path):
    out = str(tmp_path / 'work' / 'hk.txt')
    st = oc.osm_to_hkby(osm_file, out, 'Bayern')
    lines = open(out, encoding='utf-8').read().splitlines()
    assert lines[0].split(';') == oc.HK_BY_HEADER
    rows = [l.split(';') for l in lines[1:]]
    got = sorted((r[14], r[15], r[16], r[10], r[20]) for r in rows)
    assert got == [('Dorfstraße', '1', '', 'Testdorf', '85748'),
                   ('Dorfstraße', '2', 'a', 'Testdorf', '85748'),     # municipality from boundary
                   ('Kirchweg', '5', '', 'Testdorf', ''),
                   ('Kirchweg', '7', '', 'Testdorf', '')]
    assert all(len(r) == 24 and r[4] == 'Bayern' and r[3] == '09' and r[17] == '32' for r in rows)
    one = next(r for r in rows if r[15] == '1')
    assert one[8] == 'Oberdorf' and one[1].startswith('OSMN')                # point before building
    x, y = float(one[18]), float(one[19])
    assert 689000 < x < 690500 and 5330000 < y < 5340000                     # UTM 32
    assert st['adressen'] == 4 and st['doppelt'] == 1 and st['gemeinde_ergaenzt'] == 1
    assert not os.path.exists(os.path.join(os.path.dirname(out), 'osmconf_geocoder.ini'))


@needs_gdal
def test_worker_imports_osm_file_source(db, monkeypatch, osm_file):
    cat = {'version': 1, 'sources': {'Bayern': {
        'typ': 'datei', 'pfad': osm_file, 'format': 'osm', 'quelle_name': 'OSM', 'lizenz': 'ODbL'}}}
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)
    w = suw.SourceUpdateWorker('Bayern', force=True, plugin_version='t')
    logs, result = [], {}
    w.log.connect(logs.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    assert any('4 Adressen aus OpenStreetMap übernommen' in m and 'doppelte' in m for m in logs)
    conn = db.get_connection()
    rows = conn.execute("SELECT gemeinde, strasse, hausnummer, hausnr_zusatz, plz, kreis, epsg "
                        "FROM addresses WHERE bundesland='Bayern' ORDER BY strasse, hausnummer").fetchall()
    conn.close()
    assert [tuple(r[:4]) for r in rows] == [('Testdorf', 'Dorfstraße', '1', ''),
                                            ('Testdorf', 'Dorfstraße', '2', 'a'),
                                            ('Testdorf', 'Kirchweg', '5', ''),
                                            ('Testdorf', 'Kirchweg', '7', '')]
    assert rows[0][5] == 'Oberdorf' and rows[0][6] == 25832
    assert os.path.exists(osm_file)
    assert not os.path.exists(os.path.join(sd.cache_dir_for(db.DB_PATH, 'Bayern'), '_osm'))


def test_worker_osm_without_gdal(db, monkeypatch, osm_file):
    monkeypatch.setattr(oc, 'gdal_available', lambda: False)
    cat = {'version': 1, 'sources': {'Bayern': {
        'typ': 'datei', 'pfad': osm_file, 'format': 'osm', 'quelle_name': 'OSM', 'lizenz': 'ODbL'}}}
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)
    w = suw.SourceUpdateWorker('Bayern', force=True, plugin_version='t')
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'] is False and 'OpenStreetMap' in result['msg']


def test_worker_reports_lock_as_skipped(db, monkeypatch):
    """Lock after download: shown as "skipped" in the summary, not "no new data"."""
    from datetime import datetime, timedelta, timezone
    cat = {'version': 1, 'sources': {'Bayern': {'typ': 'direct', 'url': 'https://x/b.osm.pbf',
                                                'format': 'osm', 'min_abstand_tage': 28}}}
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)
    db.save_source_meta('Bayern', url='https://x/b.osm.pbf',
                        heruntergeladen_am=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
    w = suw.SourceUpdateWorker(['Bayern', 'Hessen'], plugin_version='t')
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    assert 'Bayern: übersprungen – Zuletzt vor 3 Tagen geladen' in result['msg']
    assert 'keine neuen Daten' not in result['msg']
