# -*- coding: utf-8 -*-
"""Source type "file on this computer" (typ 'datei'): catalog, detection of
unchanged files, zip extraction and import via the batch update — without
network and without QGIS. The user's file must never be modified."""
import os
import time
import zipfile
import importlib

import pytest

from conftest import PKG_NAME
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')
from test_source_downloader import _hkde_line   # noqa: E402


def _hkde_file(path, n=2, strasse='Dateiweg'):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_hkde_line('Bochum', strasse, str(i), 371000.0 + i, 5701000.0 + i)
                          for i in range(1, n + 1)) + '\n')
    return path


def _entry(path, fmt='hkde', **kw):
    e = {'typ': 'datei', 'pfad': path, 'format': fmt, 'quelle_name': 'Eigene Datei', 'lizenz': 'dl-de/by-2-0'}
    e.update(kw)
    return e


def _cat(bl, entry):
    return {'version': 1, 'user_agent': 'Test/{version}', 'sources': {bl: entry}}


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_validate_file_source():
    sd.validate_catalog({'sources': {'X': _entry('C:/daten/hk.txt')}})        # valid without URL
    with pytest.raises(sd.SourceError, match='Datei'):
        sd.validate_catalog({'sources': {'X': {'typ': 'datei', 'format': 'hkde'}}})
    with pytest.raises(sd.SourceError, match='Straße'):
        sd.validate_catalog({'sources': {'X': _entry('a.shp', 'shp', felder={'strasse': 'STR'})}})
    sd.validate_catalog({'sources': {'X': _entry('a.shp', 'shp',
                                                 felder={'strasse': 'STR', 'hausnummer': 'HNR'})}})


def test_registered_includes_files_in_table_order():
    cat = {'sources': {
        'Hessen': _entry('h.txt'),
        'Berlin': {'typ': 'atom', 'url': 'https://x', 'format': 'hkde', 'link_muster': 'x'},
        'Bayern': {'typ': 'manuell', 'url': 'https://shop', 'format': 'hkde', 'grund': 'Shop.'}}}
    assert sd.registered_bundeslaender(cat) == ['Berlin', 'Hessen']
    assert sd.automatic_bundeslaender(cat) == ['Berlin']
    assert sd.is_registered(cat['sources']['Hessen']) and not sd.is_automatic(cat['sources']['Hessen'])
    assert 'h.txt' in sd.describe(cat['sources']['Hessen'])


def test_manual_text_points_to_detail_page():
    txt = sd.manual_text('Hessen', {'typ': 'manuell', 'url': 'https://gds.hessen.de',
                                    'grund': 'Download nur nach Anmeldung.'})
    assert 'noch keine Quelle eingetragen' in txt and 'Anmeldung' in txt
    assert 'https://gds.hessen.de' in txt and 'Detailseite' in txt and 'unter 2' not in txt


# ── update_source ────────────────────────────────────────────────────────────

def test_update_source_file_unchanged_and_replaced(tmp_path):
    src = _hkde_file(str(tmp_path / 'hk.txt'))
    db_path = str(tmp_path / 'g.db')
    cat = _cat('Hessen', _entry(src))
    logs = []
    r = sd.update_source('Hessen', db_path, catalog=cat, log=logs.append)
    assert r['status'] == 'downloaded' and r['files'] == [src] and r['temp_dir'] is None
    meta = r['meta']
    assert meta['url'] == src and meta['etag'].startswith('datei:') and meta['last_modified']
    assert any(m.startswith('Datei: hk.txt') for m in logs)

    r2 = sd.update_source('Hessen', db_path, prev_meta=meta, catalog=cat)
    assert r2['status'] == 'unchanged' and 'unverändert' in r2['text']
    r3 = sd.update_source('Hessen', db_path, prev_meta=meta, catalog=cat, force=True)
    assert r3['status'] == 'downloaded'

    # Newer version at the same path → import again
    time.sleep(0.01)
    _hkde_file(src, n=3)
    os.utime(src, (time.time() + 5, time.time() + 5))
    r4 = sd.update_source('Hessen', db_path, prev_meta=meta, catalog=cat)
    assert r4['status'] == 'downloaded'

    # Different path → new
    other = _hkde_file(str(tmp_path / 'hk2.txt'))
    r5 = sd.update_source('Hessen', db_path, prev_meta=meta, catalog=_cat('Hessen', _entry(other)))
    assert r5['status'] == 'downloaded'


def test_update_source_file_missing(tmp_path):
    with pytest.raises(sd.SourceError, match='nicht gefunden'):
        sd.update_source('Hessen', str(tmp_path / 'g.db'),
                         catalog=_cat('Hessen', _entry(str(tmp_path / 'fehlt.txt'))))


def test_update_source_file_zip_extracts_to_temp(tmp_path):
    txt = _hkde_file(str(tmp_path / 'inhalt.txt'))
    zpath = str(tmp_path / 'hk.zip')
    with zipfile.ZipFile(zpath, 'w') as z:
        z.write(txt, 'daten/hk_hessen.txt')
        z.writestr('liesmich.pdf', b'%PDF')
    db_path = str(tmp_path / 'g.db')
    r = sd.update_source('Hessen', db_path, catalog=_cat('Hessen', _entry(zpath)))
    assert r['status'] == 'downloaded'
    assert [os.path.basename(f) for f in r['files']] == ['hk_hessen.txt']
    assert r['temp_dir'] == os.path.join(sd.cache_dir_for(db_path, 'Hessen'), '_entpackt')
    assert all(f.startswith(r['temp_dir']) for f in r['files'])


def test_test_source_file(tmp_path):
    ok, txt = sd.test_source(_entry(_hkde_file(str(tmp_path / 'hk.txt'))))
    assert ok and 'Datei vorhanden' in txt
    ok, txt = sd.test_source(_entry(str(tmp_path / 'fehlt.txt')))
    assert not ok and 'nicht gefunden' in txt


# ── Batch update with import ─────────────────────────────────────────────────

def _run(bl=None, force=False):
    w = suw.SourceUpdateWorker(bl, force=force, plugin_version='t')
    logs, result = [], {}
    w.log.connect(logs.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    return result, logs


def test_worker_imports_file_source_and_keeps_user_file(db, monkeypatch, tmp_path):
    # File in the cache folder next to the DB — must still never be deleted
    cache = sd.cache_dir_for(db.DB_PATH, 'Nordrhein-Westfalen')
    os.makedirs(cache, exist_ok=True)
    src = _hkde_file(os.path.join(cache, 'meine_hk.txt'), n=3)
    cat = _cat('Nordrhein-Westfalen', _entry(src))
    cat['sources']['Hessen'] = {'typ': 'manuell', 'url': 'https://shop', 'format': 'hkde', 'grund': 'Shop.'}
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)

    result, logs = _run()                         # all registered: only NRW (file)
    assert result['ok'], result
    assert 'Hessen' not in result['msg'] and 'Nordrhein-Westfalen: 3 Adressen importiert' in result['msg']
    conn = db.get_connection()
    rows = conn.execute("SELECT strasse, hausnummer FROM addresses WHERE bundesland='Nordrhein-Westfalen' "
                        "ORDER BY hausnummer").fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [('Dateiweg', '1'), ('Dateiweg', '2'), ('Dateiweg', '3')]
    assert os.path.exists(src)                    # user's file is kept
    meta = db.get_source_meta('Nordrhein-Westfalen')
    assert meta['url'] == src and meta['adressen'] == 3

    result2, _ = _run()
    assert result2['ok'] and 'unverändert' in result2['msg']

    result3, _ = _run('Nordrhein-Westfalen', force=True)     # "Import file"
    assert result3['ok'] and '3 Adressen importiert' in result3['msg']
    assert os.path.exists(src)


def test_worker_zip_file_source_cleans_only_temp(db, monkeypatch, tmp_path):
    txt = _hkde_file(str(tmp_path / 'x.txt'), n=2)
    zpath = str(tmp_path / 'hk.zip')
    with zipfile.ZipFile(zpath, 'w') as z:
        z.write(txt, 'hk.txt')
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: _cat('Nordrhein-Westfalen', _entry(zpath)))
    result, _ = _run('Nordrhein-Westfalen', force=True)
    assert result['ok'], result
    assert os.path.exists(zpath)
    assert not os.path.exists(os.path.join(sd.cache_dir_for(db.DB_PATH, 'Nordrhein-Westfalen'), '_entpackt'))


def test_worker_single_unregistered_state_explains(db, monkeypatch):
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: _cat(
        'Hessen', {'typ': 'manuell', 'url': 'https://gds.hessen.de', 'format': 'hkde',
                   'grund': 'Download nur nach Anmeldung.'}))
    result, _ = _run('Hessen')
    assert result['ok'] is False and 'Detailseite' in result['msg']


# ── Zip inside zip (Bremen) ──────────────────────────────────────────────────

def test_extract_nested_zip(tmp_path):
    inner = tmp_path / 'inner.zip'
    with zipfile.ZipFile(inner, 'w') as z:
        for ext in ('shp', 'shx', 'dbf', 'prj', 'cpg'):
            z.writestr(f'AX_GeoreferenzierteGebaeudeadresse.{ext}', b'x')
        z.writestr('liesmich.pdf', b'%PDF')
    outer = tmp_path / 'GEBAEUDEADRESSEN_SHP_FHB.zip'
    with zipfile.ZipFile(outer, 'w') as z:
        z.write(inner, 'GEBAEUDEADRESSEN_SHP_FHB_2026_08.zip')
    dest = tmp_path / 'out'
    dest.mkdir()
    files = sd.extract_data_files(str(outer), str(dest), r'\.(shp|shx|dbf|prj|cpg)$')
    assert sorted(os.path.basename(f) for f in files) == [
        'AX_GeoreferenzierteGebaeudeadresse.' + e for e in ('cpg', 'dbf', 'prj', 'shp', 'shx')]
    assert not (dest / 'GEBAEUDEADRESSEN_SHP_FHB_2026_08.zip').exists()     # inner zip cleaned up


def test_bremen_catalog_entry():
    e = sd.load_catalog(include_user=False)['sources']['Bremen']
    assert (e['typ'], e['format']) == ('direct', 'shp')
    assert e['url'].startswith('https://gdi2.geo.bremen.de/inspire/download/ALKIS_Gebaeudeadressen/')
    assert e['felder'] == {'strasse': 'stn', 'hausnummer': 'hnr', 'hausnr_zusatz': 'adz',
                           'plz': 'plz', 'gemeinde': 'onm'}
    assert 'Bremen' in sd.automatic_bundeslaender()


# ── Protection against empty or wrong files ─────────────────────────────────

def test_empty_file_keeps_existing_addresses(db, monkeypatch, tmp_path):
    bad = tmp_path / 'wartung.txt'
    bad.write_text('<html><body>Dieser Dienst ist im Moment nicht verfügbar</body></html>\n',
                   encoding='utf-8')
    monkeypatch.setattr(sd, 'load_catalog',
                        lambda path=None: _cat('Nordrhein-Westfalen', _entry(str(bad))))
    before = db.get_db_stats()['Nordrhein-Westfalen']
    result, _ = _run('Nordrhein-Westfalen', force=True)
    assert result['ok'] is False and 'bleibt erhalten' in result['msg']
    assert db.get_db_stats()['Nordrhein-Westfalen'] == before


def test_html_instead_of_file_is_rejected(tmp_path):
    from test_source_downloader import FakeHttp
    http = FakeHttp({'https://x/hk.zip': (206, {'content-type': 'text/html'}, b'<html>Wartung</html>')})
    with pytest.raises(sd.SourceError, match='Webseite'):
        sd.download_file(http, 'https://x/hk.zip', str(tmp_path))
    assert not (tmp_path / 'hk.zip').exists()


# ── Fallback source ──────────────────────────────────────────────────────────

def test_fallback_source_used_and_guarded(db, monkeypatch, tmp_path):
    good = _hkde_file(str(tmp_path / 'ersatz.txt'), n=2, strasse='Ersatzweg')
    entry = _entry(str(tmp_path / 'fehlt.txt'))              # main source: file missing
    entry['ausweich'] = {'typ': 'direct', 'url': 'https://x/ersatz.txt', 'format': 'hkde'}
    from test_source_downloader import FakeHttp
    http = FakeHttp({'https://x/ersatz.txt': open(good, 'rb').read()})
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: http)
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: _cat('Nordrhein-Westfalen', entry))
    result, logs = _run('Nordrhein-Westfalen', force=True)
    assert result['ok'], result
    assert any('Ausweichquelle' in m for m in logs)
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland='Nordrhein-Westfalen' "
                        "AND strasse='Ersatzweg'").fetchone()[0] == 2
    conn.close()
    # With 'nur_ohne_bestand', existing data is kept
    entry['ausweich']['nur_ohne_bestand'] = True
    result, _ = _run('Nordrhein-Westfalen', force=True)
    assert result['ok'] is False and 'noch keine Adressen' in result['msg']
    assert db.get_db_stats()['Nordrhein-Westfalen'] == 2
