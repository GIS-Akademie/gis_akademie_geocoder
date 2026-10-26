# -*- coding: utf-8 -*-
"""OGC API Features (Saarland): GeoJSON pages → address rows → import."""
import json
import importlib

from conftest import PKG_NAME
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')
from test_source_downloader import FakeHttp   # noqa: E402

BASE = 'https://x/collections/hk/items'


def _feat(i, stn, hnr, adz='', plz='66111', onm='Saarbrücken', lon=6.99, lat=49.23):
    return {'type': 'Feature', 'id': f'hk.{i}', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {'STN': stn, 'HNR': hnr, 'ADZ': adz, 'PLZ': plz, 'ONM': onm}}


def _page(feats, total):
    return json.dumps({'type': 'FeatureCollection', 'numberMatched': total,
                       'features': feats}).encode('utf-8')


def _catalog():
    return {'version': 1, 'sources': {'Saarland': {
        'typ': 'ogcapi', 'url': BASE, 'format': 'geojson', 'seitengroesse': 2,
        'felder': {'strasse': 'STN', 'hausnummer': 'HNR', 'hausnr_zusatz': 'ADZ', 'plz': 'PLZ',
                   'gemeinde': 'ONM'},
        'quelle_name': 'LVGL', 'lizenz': 'dl-de/by-2-0'}}}


def _http():
    return FakeHttp({
        f'{BASE}?f=json&limit=2&offset=0': _page([_feat(1, 'Bahnhofstraße', '1'),
                                                  _feat(2, 'Bahnhofstraße', '2', 'a')], 3),
        f'{BASE}?f=json&limit=2&offset=2': _page([_feat(3, 'Am Markt', '5;7', onm='Neunkirchen')], 3),
    })


def test_ogcapi_pages_to_addresses(db, monkeypatch):
    http = _http()
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: _catalog())
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: http)
    w = suw.SourceUpdateWorker('Saarland', plugin_version='t')
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    conn = db.get_connection()
    rows = conn.execute("SELECT strasse, hausnummer, hausnr_zusatz, plz, gemeinde, epsg, x, y "
                        "FROM addresses WHERE bundesland='Saarland' ORDER BY strasse, hausnummer").fetchall()
    conn.close()
    assert [tuple(r[:5]) for r in rows] == [
        ('Am Markt', '5', '', '66111', 'Neunkirchen'), ('Am Markt', '7', '', '66111', 'Neunkirchen'),
        ('Bahnhofstraße', '1', '', '66111', 'Saarbrücken'), ('Bahnhofstraße', '2', 'a', '66111', 'Saarbrücken')]
    assert rows[0][5] == 25832 and 350000 < rows[0][6] < 360000 and 5450000 < rows[0][7] < 5460000

    # Second run with identical content: same checksum → no import
    w2 = suw.SourceUpdateWorker('Saarland', plugin_version='t')
    r2 = {}
    w2.finished.connect(lambda ok, msg: r2.update(ok=ok, msg=msg))
    w2.run()
    assert r2['ok'] and r2['msg'] == 'Keine neuen Daten seit dem letzten Import.'


def test_ogcapi_incomplete_download_keeps_data(db, monkeypatch):
    http = FakeHttp({f'{BASE}?f=json&limit=2&offset=0': _page([_feat(1, 'A-Straße', '1')], 3)})
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: _catalog())
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: http)
    before = db.get_db_stats().get('Saarland')
    w = suw.SourceUpdateWorker('Saarland', plugin_version='t')
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'] is False and 'unvollständig' in result['msg']
    assert db.get_db_stats().get('Saarland') == before
