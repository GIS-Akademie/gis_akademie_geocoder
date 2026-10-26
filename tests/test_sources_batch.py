# -*- coding: utf-8 -*-
"""User sources (catalog merge), batch update, WFS lock after download,
postcode coverage and postcode log — without network and without QGIS."""
import json
import sqlite3
import hashlib
import importlib
from datetime import datetime, timedelta

import pytest

from conftest import lg, PKG_NAME
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')
from test_source_downloader import FakeHttp, _zip_bytes, _hkde_line   # noqa: E402


@pytest.fixture
def user_catalog(tmp_path):
    path = str(tmp_path / 'profil' / 'sources_user.json')
    old = sd.USER_CATALOG_PATH
    sd.set_user_catalog_path(path)
    yield path
    sd.set_user_catalog_path(old)


# ── User sources ─────────────────────────────────────────────────────────────

def test_user_source_overrides_and_removes(user_catalog):
    standard = sd.load_catalog()['sources']['Brandenburg']
    assert standard['_herkunft'] == 'standard'
    saved = sd.save_user_source('Brandenburg', {
        'typ': 'direct', 'url': 'https://x/hk_bb.zip', 'format': 'hkde',
        'entpacken': r'\.txt$', 'quelle_name': 'LGB Direktlink', 'lizenz': 'dl-de/by-2-0',
        'hinweis': '', '_herkunft': 'standard'})
    assert '_herkunft' not in saved and 'hinweis' not in saved          # cleaned
    cat = sd.load_catalog()
    e = cat['sources']['Brandenburg']
    assert (e['typ'], e['_herkunft']) == ('direct', 'eigene')
    assert 'Brandenburg' in sd.automatic_bundeslaender(cat)
    assert cat['sources']['Berlin']['_herkunft'] == 'standard'
    assert json.load(open(user_catalog, encoding='utf-8'))['sources']['Brandenburg']['url'] == 'https://x/hk_bb.zip'
    assert sd.remove_user_source('Brandenburg') is True
    assert sd.load_catalog()['sources']['Brandenburg']['url'] == standard['url']
    assert sd.remove_user_source('Brandenburg') is False


def test_invalid_user_source_rejected_and_skipped(user_catalog):
    with pytest.raises(sd.SourceError):
        sd.save_user_source('Thüringen', {'typ': 'page_link', 'url': 'https://x', 'format': 'hkde'})
    # file broken by hand: the default stays active, the error is reported
    import os
    os.makedirs(os.path.dirname(user_catalog), exist_ok=True)
    json.dump({'sources': {'Thüringen': {'typ': 'ftp', 'url': 'x', 'format': 'hkde'}}},
              open(user_catalog, 'w', encoding='utf-8'))
    cat = sd.load_catalog()
    assert cat['sources']['Thüringen']['_herkunft'] == 'standard'
    assert cat['sources']['Thüringen']['url'] == sd.load_catalog(include_user=False)['sources']['Thüringen']['url']
    assert any('Thüringen' in f for f in cat['_fehler'])


def test_catalog_lists_all_states_and_plz_flags():
    cat = sd.load_catalog(include_user=False)
    assert set(cat['sources']) == set(sd.BUNDESLAENDER)
    assert cat['sources']['Nordrhein-Westfalen']['liefert_plz'] is False
    assert cat['sources']['Berlin']['liefert_plz'] is True
    assert cat['sources']['Mecklenburg-Vorpommern']['min_abstand_tage'] == 28
    hh = cat['sources']['Hamburg']
    assert (hh['typ'], hh['gemeinde_ebene'], hh['liefert_plz']) == ('wfs', '2ndOrder', True)
    assert hh['url'].startswith('https://geodienste.hamburg.de/HH_WFS_INSPIRE_Adressen')
    assert 'status' not in hh


# ── test_source (editor "Quelle testen") ─────────────────────────────────────

def test_test_source_direct_and_wfs():
    http = FakeHttp({'https://x/a.zip': (200, {'content-length': '2500000',
                                               'last-modified': 'Wed, 25 Mar 2026 08:32:06 GMT'}, b'PK')})
    ok, txt = sd.test_source({'typ': 'direct', 'url': 'https://x/a.zip', 'format': 'hkde'}, http=http)
    assert ok and 'a.zip' in txt and '2,5 MB' in txt and 'Stand 25.03.2026' in txt
    ok, txt = sd.test_source({'typ': 'page_link', 'url': 'https://x', 'format': 'hkde'}, http=http)
    assert not ok and 'ungültig' in txt

    class H(FakeHttp):
        def get(self, url, headers=None, max_bytes=None):
            assert 'resultType=hits' in url
            return 200, {}, b'<wfs:FeatureCollection numberMatched="518416"/>'
    ok, txt = sd.test_source({'typ': 'wfs', 'url': 'https://wfs', 'format': 'gml',
                              'typnamen': ['ad:Address']}, http=H({}))
    assert ok and '518.416' in txt


# ── Download: same checksum with a new URL ───────────────────────────────────

def test_same_content_new_url_is_unchanged(tmp_path):
    body = b'inhalt'
    http = FakeHttp({'https://x/token2/hk.zip': (200, {}, body)})
    prev = {'url': 'https://x/token1/hk.zip', 'sha256': hashlib.sha256(body).hexdigest()}
    info = sd.download_file(http, 'https://x/token2/hk.zip', str(tmp_path), prev_meta=prev)
    assert info['status'] == 'unchanged'


# ── WFS: lock after download and same checksum ──────────────────────────────

def _wfs_catalog(tage=28):
    return {'version': 1, 'user_agent': 'T/{version}', 'sources': {'Testland': {
        'typ': 'wfs', 'url': 'https://wfs/x', 'format': 'gml', 'typnamen': ['ad:Address'],
        'seitengroesse': 10, 'min_abstand_tage': tage}}}


def test_wfs_min_interval_skips_download(tmp_path):
    http = FakeHttp({})
    recent = (datetime.now().astimezone() - timedelta(days=3)).isoformat(timespec='seconds')
    r = sd.update_source('Testland', str(tmp_path / 'g.db'), prev_meta={'heruntergeladen_am': recent},
                         catalog=_wfs_catalog(), http=http)
    assert r['status'] == 'skipped' and '3 Tag' in r['text'] and 'übersprungen' in r['text']
    assert http.requests == []                                   # nothing requested


def test_wfs_same_hash_unchanged_and_force(tmp_path):
    from fake_wfs import FakeWfs
    http = FakeWfs(n_addresses=15, max_count=10)               # stable server
    old = (datetime.now().astimezone() - timedelta(days=40)).isoformat(timespec='seconds')
    r1 = sd.update_source('Testland', str(tmp_path / 'g.db'), prev_meta={'heruntergeladen_am': old},
                          catalog=_wfs_catalog(), http=http)
    assert r1['status'] == 'downloaded' and r1['meta']['sha256']
    prev = dict(r1['meta'], heruntergeladen_am=old)
    r2 = sd.update_source('Testland', str(tmp_path / 'g.db'), prev_meta=prev,
                          catalog=_wfs_catalog(), http=http)
    assert r2['status'] == 'unchanged' and r2['text'] == 'Keine neuen Daten seit dem letzten Import.'
    r3 = sd.update_source('Testland', str(tmp_path / 'g.db'), prev_meta=prev, force=True,
                          catalog=_wfs_catalog(), http=http)
    assert r3['status'] == 'downloaded'


# ── Batch update ─────────────────────────────────────────────────────────────

def _hkde_zip(strasse, n, land='Nordrhein-Westfalen'):
    lines = []
    for i in range(1, n + 1):
        line = _hkde_line('Bochum', strasse, str(i), 371000.0 + i, 5701000.0 + i).split(';')
        line[4] = land
        lines.append(';'.join(line))
    return _zip_bytes({'daten.txt': ('\n'.join(lines) + '\n').encode('utf-8')})


def test_batch_update_summary_and_single_lookup_rebuild(db, monkeypatch):
    cat = {'version': 1, 'user_agent': 'T/{version}', 'sources': {
        'Nordrhein-Westfalen': {'typ': 'direct', 'url': 'https://x/nrw.zip', 'format': 'hkde',
                                'entpacken': r'\.txt$', 'quelle_name': 'NRW-Test'},
        'Berlin':              {'typ': 'direct', 'url': 'https://x/be.zip', 'format': 'hkde',
                                'entpacken': r'\.txt$', 'quelle_name': 'BE-Test'},
        'Sachsen':             {'typ': 'direct', 'url': 'https://x/fehlt.zip', 'format': 'hkde',
                                'quelle_name': 'kaputt'},
        'Hessen':              {'typ': 'manuell', 'url': 'https://shop', 'format': 'hkde',
                                'grund': 'Shop'},
    }}
    nrw = _hkde_zip('Neuweg', 3)
    be  = _hkde_zip('Spreeweg', 2, land='Berlin')
    http = FakeHttp({'https://x/nrw.zip': (200, {'etag': '"n1"'}, nrw),
                     'https://x/be.zip': lambda h: ((304, {}, b'') if h.get('If-None-Match') == '"b1"'
                                                    else (200, {'etag': '"b1"'}, be))})
    monkeypatch.setattr(sd, 'load_catalog', lambda *a, **k: cat)
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: http)
    lg.save_source_meta('Berlin', url='https://x/be.zip', etag='"b1"')   # Berlin unchanged

    calls = []
    real = lg.rebuild_lookup_tables
    monkeypatch.setattr(lg, 'rebuild_lookup_tables', lambda conn, log=None: (calls.append(1), real(conn, log)))

    w = suw.SourceUpdateWorker(None, plugin_version='t')        # None = all automatic ones
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()

    st = {bl: s for bl, s, _t in w.summary}
    assert st == {'Nordrhein-Westfalen': 'aktualisiert', 'Berlin': 'unverändert',
                  'Sachsen': 'fehler'}                          # Hessen (manual) not attempted at all
    assert result['ok'] is False and 'Quellen-Aktualisierung' in result['msg']
    assert len(calls) == 1                                       # directory built only once
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland='Nordrhein-Westfalen'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland='Berlin'").fetchone()[0] == 4  # unchanged
    conn.close()
    assert lg.get_source_meta('Nordrhein-Westfalen')['adressen'] == 3
    assert lg.get_plz_coverage()['Nordrhein-Westfalen'] == (3, 3)  # new data without postcode


def test_batch_explicit_manual_state_is_skipped(db, monkeypatch):
    cat = {'version': 1, 'sources': {'Hessen': {'typ': 'manuell', 'url': 'u', 'format': 'hkde', 'grund': 'Shop'},
                                     'Berlin': {'typ': 'manuell', 'url': 'u', 'format': 'hkde', 'grund': 'x'}}}
    monkeypatch.setattr(sd, 'load_catalog', lambda *a, **k: cat)
    w = suw.SourceUpdateWorker(['Hessen', 'Berlin'])
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert [s for _b, s, _t in w.summary] == ['manuell', 'manuell'] and result['ok'] is True


# ── Postcode coverage and postcode log ───────────────────────────────────────

def test_plz_coverage_from_lookup(db):
    cov = db.get_plz_coverage()
    assert cov['Berlin'] == (4, 0)
    assert cov['Bayern'][1] == 0
    conn = db.get_connection()
    conn.execute("UPDATE addresses SET plz = '' WHERE bundesland = 'Hamburg'")
    db.rebuild_lookup_tables(conn)
    conn.commit()
    conn.close()
    assert db.get_plz_coverage()['Hamburg'] == (4, 4)


def test_plz_coverage_none_for_old_schema(db):
    conn = db.get_connection()
    conn.execute('DROP TABLE bundesland_index')
    conn.commit()
    conn.close()
    assert db.get_plz_coverage() is None


def test_record_plz_meta(db):
    conn = sqlite3.connect(db.DB_PATH)
    info = {'layer': 'OSM_PLZ.gpkg (plz)', 'feld': 'plz', 'ergaenzt': 10, 'ohne_flaeche': 2}
    db.record_plz_meta(conn, 'Berlin', info, replace=True)
    db.record_plz_meta(conn, 'Berlin', dict(info, ergaenzt=5, ohne_flaeche=1), replace=False)  # append
    conn.commit()
    m = db.all_plz_meta()['Berlin']
    assert (m['layer'], m['ergaenzt'], m['ohne_flaeche']) == ('OSM_PLZ.gpkg (plz)', 15, 3)
    db.record_plz_meta(conn, 'Berlin', None, replace=False)        # addition without layer: kept
    conn.commit()
    assert 'Berlin' in db.all_plz_meta()
    db.record_plz_meta(conn, 'Berlin', None, replace=True)         # replaced without layer: gone
    conn.commit()
    conn.close()
    assert 'Berlin' not in db.all_plz_meta()


def test_resolver_info():
    class R:
        def stats(self):
            return {'matched': 7, 'missed': 1}
    assert lg.resolver_info(None, 'x', 'plz') is None
    assert lg.resolver_info(R(), r'C:\daten\plz.gpkg', 'plz', 'OSM_PLZ') == {
        'layer': 'plz.gpkg (OSM_PLZ)', 'feld': 'plz', 'ergaenzt': 7, 'ohne_flaeche': 1}


def test_read_helpers_do_not_create_tables(db):
    assert db.get_source_meta('Berlin') is None
    assert db.all_plz_meta() == {}
    conn = db.get_connection()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert 'source_meta' not in names and 'plz_meta' not in names
