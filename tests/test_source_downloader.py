# -*- coding: utf-8 -*-
"""Tests for source_downloader.py and source_update_worker.py — without network:
a FakeHttp answers the requests from a dict."""
import os
import io
import json
import zipfile
import hashlib

import pytest

from conftest import lg, PKG_NAME
import importlib
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')


class FakeHttp:
    """Responses: url -> bytes | (status, headers, bytes). Records the requests."""
    def __init__(self, responses):
        self.responses = responses
        self.requests  = []

    def _lookup(self, url, headers):
        self.requests.append((url, dict(headers or {})))
        if url not in self.responses:
            raise sd.SourceError(f'HTTP 404 bei {url}')
        r = self.responses[url]
        if callable(r):
            r = r(headers or {})
        if isinstance(r, bytes):
            return 200, {}, r
        return r

    def get(self, url, headers=None, max_bytes=None):
        return self._lookup(url, headers)

    def download(self, url, dest, headers=None, progress=None, cancel=None):
        status, hdrs, body = self._lookup(url, headers)
        if status == 304:
            return 304, hdrs
        if cancel and cancel():
            raise sd.Cancelled()
        with open(dest, 'wb') as f:
            f.write(body)
        if progress:
            progress(len(body), len(body))
        h = dict(hdrs)
        h['_sha256'] = hashlib.sha256(body).hexdigest()
        h['_size']   = str(len(body))
        return status, h


def _zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_catalog_loads_and_covers_all_states():
    cat = sd.load_catalog()
    expected = set(lg._PLZ_PREFIX_GROUPS) | {'Bremen'}
    assert set(cat['sources']) == expected
    auto = sd.automatic_bundeslaender(cat)
    assert {'Nordrhein-Westfalen', 'Berlin', 'Sachsen', 'Mecklenburg-Vorpommern'} <= set(auto)
    assert {'Rheinland-Pfalz', 'Baden-Württemberg', 'Hessen', 'Niedersachsen'} <= set(auto)
    for e in cat['sources'].values():
        if e['typ'] == 'manuell':
            assert 'manuell' in sd.describe(e) and e.get('grund')
    assert 'Lizenz' in sd.describe(cat['sources']['Berlin'])


@pytest.mark.parametrize('bad', [
    {'typ': 'ftp', 'format': 'hkde', 'url': 'x'},
    {'typ': 'direct', 'format': 'xlsx', 'url': 'x'},
    {'typ': 'direct', 'format': 'hkde'},
    {'typ': 'index_json', 'format': 'hkde', 'url': 'x'},
    {'typ': 'page_link', 'format': 'hkde', 'url': 'x'},
    {'typ': 'manuell', 'format': 'hkde', 'url': 'x'},
    {'typ': 'direct', 'format': 'hkde', 'url': 'x', 'entpacken': '('},
])
def test_catalog_validation_rejects_bad_entries(bad):
    with pytest.raises((sd.SourceError, Exception)):
        sd.validate_catalog({'sources': {'Testland': bad}})


def test_cache_dir_is_next_to_db(tmp_path):
    d = sd.cache_dir_for(str(tmp_path / 'geocoder.db'), 'Baden-Württemberg')
    assert d == os.path.join(str(tmp_path), 'quellen', 'Baden-W_rttemberg')


# ── URL resolution ───────────────────────────────────────────────────────────

def test_resolve_direct():
    e = {'typ': 'direct', 'url': 'https://x/a.zip'}
    assert sd.resolve_download_urls(e, FakeHttp({})) == ['https://x/a.zip']


def test_resolve_index_json():
    idx = json.dumps({'datasets': [{'name': 'gebref', 'files': [
        {'name': 'gebref_EPSG25832_ASCII.zip', 'size': '1'},
        {'name': 'gebref_EPSG25832_GPKG.zip', 'size': '1'}]}]}).encode()
    http = FakeHttp({'https://x/akt/gebref_txt/index.json': idx})
    e = {'typ': 'index_json', 'url': 'https://x/akt/gebref_txt/index.json',
         'datei_muster': r'^gebref_EPSG25832_ASCII\.zip$'}
    assert sd.resolve_download_urls(e, http) == ['https://x/akt/gebref_txt/gebref_EPSG25832_ASCII.zip']
    e['datei_muster'] = r'nix'
    with pytest.raises(sd.SourceError):
        sd.resolve_download_urls(e, http)


def test_resolve_page_and_feed_links():
    page = b'<a href="https://geocloud.x/public.php/dav/files/TOKEN/hk_sn_ascii.zip">Download</a> <a href="/other.pdf">x</a>'
    http = FakeHttp({'https://x/seite.html': page, 'https://x/feed.atom': page})
    e = {'typ': 'page_link', 'url': 'https://x/seite.html',
         'link_muster': r'https://geocloud\.x/[^"\' ]*hk_sn_ascii\.zip'}
    assert sd.resolve_download_urls(e, http) == ['https://geocloud.x/public.php/dav/files/TOKEN/hk_sn_ascii.zip']
    e = {'typ': 'atom', 'url': 'https://x/feed.atom', 'link_muster': r'href="(/other\.pdf)"'}
    assert sd.resolve_download_urls(e, http) == ['https://x/other.pdf']   # relative → absolute


def test_resolve_ckan_picks_newest_resource():
    data = {'result': {'results': [{'resources': [
        {'url': 'https://a/alkis_adressen_hh_2018-04-01_x.GML'},
        {'url': 'https://a/alkis_adressen_hh_2019-10-01_x.GML'},
        {'url': 'https://a/alkis_adressen_hh_2016-09_x.ZIP'},
        {'url': 'https://a/wms_capabilities.XML'}]}]}}
    base = 'https://ckan/api/3/action/package_search'
    http = FakeHttp({})
    http.responses = {k: json.dumps(data).encode() for k in [base]}
    # FakeHttp compares exact URLs → strip the query
    class H(FakeHttp):
        def get(self, url, headers=None, max_bytes=None):
            return super().get(url.split('?')[0], headers)
    http = H({base: json.dumps(data).encode()})
    e = {'typ': 'ckan', 'url': base, 'abfrage': 'title:"ALKIS Adressen Hamburg"',
         'ressource_muster': r'alkis_adressen_hh_(\d{4}-\d{2}(?:-\d{2})?)[^"]*\.(?:gml|zip)$'}
    assert sd.resolve_download_urls(e, http) == ['https://a/alkis_adressen_hh_2019-10-01_x.GML']


# ── Download / extraction ────────────────────────────────────────────────────

def test_download_conditional_and_unchanged(tmp_path):
    body = b'hallo'
    def responder(headers):
        if headers.get('If-None-Match') == '"e1"':
            return 304, {}, b''
        return 200, {'etag': '"e1"', 'last-modified': 'Wed, 25 Mar 2026 08:32:06 GMT'}, body
    http = FakeHttp({'https://x/a.zip': responder})
    info = sd.download_file(http, 'https://x/a.zip', str(tmp_path))
    assert info['status'] == 'ok' and info['etag'] == '"e1"' and info['size'] == 5
    assert os.path.exists(info['path'])
    # second fetch with previous state → 304 → unchanged
    info2 = sd.download_file(http, 'https://x/a.zip', str(tmp_path), prev_meta={'url': 'https://x/a.zip', 'etag': '"e1"'})
    assert info2['status'] == 'unchanged' and info2['path'] == info['path']
    assert http.requests[-1][1]['If-None-Match'] == '"e1"'
    # force → no conditional header
    info3 = sd.download_file(http, 'https://x/a.zip', str(tmp_path), prev_meta={'url': 'https://x/a.zip', 'etag': '"e1"'}, force=True)
    assert info3['status'] == 'ok' and 'If-None-Match' not in http.requests[-1][1]


def test_download_same_hash_counts_as_unchanged(tmp_path):
    http = FakeHttp({'https://x/a.zip': (200, {}, b'daten')})
    sha = hashlib.sha256(b'daten').hexdigest()
    info = sd.download_file(http, 'https://x/a.zip', str(tmp_path), prev_meta={'url': 'https://x/a.zip', 'sha256': sha})
    assert info['status'] == 'unchanged'


def test_extract_data_files(tmp_path):
    z = tmp_path / 'a.zip'
    z.write_bytes(_zip_bytes({'daten/gebref.txt': b'1;2', 'lies_mich.pdf': b'x', '../../boese.txt': b'y'}))
    out = sd.extract_data_files(str(z), str(tmp_path), r'\.txt$')
    names = sorted(os.path.basename(p) for p in out)
    assert names == ['boese.txt', 'gebref.txt']
    assert all(os.path.dirname(p) == str(tmp_path) for p in out)      # no zip slip
    with pytest.raises(sd.SourceError):
        sd.extract_data_files(str(z), str(tmp_path), r'\.csv$')
    plain = tmp_path / 'plain.txt'
    plain.write_text('x')
    assert sd.extract_data_files(str(plain), str(tmp_path)) == [str(plain)]


# ── WFS ──────────────────────────────────────────────────────────────────────

def _wfs_page(n):
    members = ''.join(f'<wfs:member><ad:Address gml:id="A{i}"/></wfs:member>' for i in range(n))
    return (f'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
            f'xmlns:ad="http://inspire.ec.europa.eu/schemas/ad/4.0" '
            f'xmlns:gml="http://www.opengis.net/gml/3.2" numberReturned="0">{members}</wfs:FeatureCollection>').encode()


# WFS fetching (paging, tiles, IDs, cancellation): see test_wfs_fetch.py


# ── Orchestration ────────────────────────────────────────────────────────────

def _catalog(entry):
    return {'version': 1, 'user_agent': 'Test/{version}', 'sources': {'Testland': entry}}


def test_update_source_manual(tmp_path):
    cat = _catalog({'typ': 'manuell', 'url': 'https://shop', 'format': 'hkde', 'grund': 'Shop'})
    r = sd.update_source('Testland', str(tmp_path / 'g.db'), catalog=cat, http=FakeHttp({}))
    assert r['status'] == 'manual' and 'Shop' in r['text']


def test_update_source_download_and_unchanged(tmp_path):
    zip_body = _zip_bytes({'hk.txt': b'X;' * 19 + b'X'})
    def responder(headers):
        if headers.get('If-None-Match') == '"v1"':
            return 304, {}, b''
        return 200, {'etag': '"v1"'}, zip_body
    http = FakeHttp({'https://x/hk.zip': responder})
    cat = _catalog({'typ': 'direct', 'url': 'https://x/hk.zip', 'format': 'hkde',
                    'entpacken': r'\.txt$', 'quelle_name': 'Testquelle'})
    logs = []
    r = sd.update_source('Testland', str(tmp_path / 'g.db'), catalog=cat, http=http, log=logs.append)
    assert r['status'] == 'downloaded'
    assert [os.path.basename(f) for f in r['files']] == ['hk.txt']
    assert r['meta']['etag'] == '"v1"' and r['meta']['dateien'] == 'hk.txt'
    assert os.path.dirname(r['files'][0]) == sd.cache_dir_for(str(tmp_path / 'g.db'), 'Testland')
    r2 = sd.update_source('Testland', str(tmp_path / 'g.db'), prev_meta=r['meta'], catalog=cat, http=http)
    assert r2['status'] == 'unchanged'
    r3 = sd.update_source('Testland', str(tmp_path / 'g.db'), prev_meta=r['meta'], force=True, catalog=cat, http=http)
    assert r3['status'] == 'downloaded'


# ── source_meta in the DB ────────────────────────────────────────────────────

def test_source_meta_roundtrip(db):
    assert db.get_source_meta('Berlin') is None
    db.save_source_meta('Berlin', url='https://x', etag='"e"', adressen=5, unbekannt='ignoriert')
    m = db.get_source_meta('Berlin')
    assert (m['url'], m['etag'], m['adressen']) == ('https://x', '"e"', 5)
    db.save_source_meta('Berlin', etag='"f"')
    assert db.get_source_meta('Berlin')['etag'] == '"f"' and db.get_source_meta('Berlin')['url'] == 'https://x'
    assert set(db.all_source_meta()) == {'Berlin'}


# ── Worker end-to-end (download → HK-DE import → source_meta) ───────────────

def _hkde_line(gemeinde, strasse, hnr, x, y):
    p = [''] * 20
    p[0] = 'X'; p[4] = 'Nordrhein-Westfalen'; p[8] = 'Bochum'; p[10] = gemeinde
    p[14] = strasse; p[15] = hnr; p[17] = '32'; p[18] = str(x); p[19] = str(y)
    return ';'.join(p)


def test_source_update_worker_end_to_end(db, monkeypatch, tmp_path):
    body = _zip_bytes({'gebref.txt': ('\n'.join([
        _hkde_line('Bochum', 'Quellenweg', '1', 371000.0, 5701000.0),
        _hkde_line('Bochum', 'Quellenweg', '2', 371010.0, 5701010.0)]) + '\n').encode('utf-8')})
    http = FakeHttp({'https://x/gebref.zip': (200, {'etag': '"q1"', 'last-modified': 'Tue, 28 Jul 2026 09:37:57 GMT'}, body)})
    cat  = _catalog({'typ': 'direct', 'url': 'https://x/gebref.zip', 'format': 'hkde',
                     'entpacken': r'\.txt$', 'quelle_name': 'Testquelle', 'lizenz': 'dl-de/by-2-0'})
    cat['sources']['Nordrhein-Westfalen'] = cat['sources'].pop('Testland')
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: http)

    before = db.get_db_stats()['Nordrhein-Westfalen']
    w = suw.SourceUpdateWorker('Nordrhein-Westfalen', force=False, replace=True, plugin_version='t')
    logs, details, result = [], [], {}
    w.log.connect(logs.append)
    w.detail.connect(details.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    conn = db.get_connection()
    rows = conn.execute("SELECT strasse, hausnummer FROM addresses WHERE bundesland='Nordrhein-Westfalen' ORDER BY hausnummer").fetchall()
    conn.close()
    assert [(r[0], r[1]) for r in rows] == [('Quellenweg', '1'), ('Quellenweg', '2')]   # existing data replaced
    assert before > 2
    meta = db.get_source_meta('Nordrhein-Westfalen')
    assert meta['etag'] == '"q1"' and meta['adressen'] == 2 and meta['importiert_am']
    assert any(m.startswith('Download abgeschlossen (') for m in logs)
    # Downloaded files cleaned up after successful import
    import os as _os
    assert not _os.path.exists(sd.cache_dir_for(db.DB_PATH, 'Nordrhein-Westfalen'))
    assert any('Download-Dateien gelöscht' in m for m in details)
    assert not any('gelöscht' in m and 'Download' in m for m in logs)

    # Second run: unchanged → no import, existing data is kept
    http.responses['https://x/gebref.zip'] = lambda h: (304, {}, b'') if h.get('If-None-Match') == '"q1"' else (200, {'etag': '"q1"'}, body)
    w2 = suw.SourceUpdateWorker('Nordrhein-Westfalen', plugin_version='t')
    result2 = {}
    w2.finished.connect(lambda ok, msg: result2.update(ok=ok, msg=msg))
    w2.run()
    assert result2['ok'] and result2['msg'] == 'Keine neuen Daten seit dem letzten Import.'


def test_source_update_worker_manual_source(db, monkeypatch):
    cat = _catalog({'typ': 'manuell', 'url': 'https://shop', 'format': 'hkde', 'grund': 'Shop'})
    cat['sources']['Hessen'] = cat['sources'].pop('Testland')
    monkeypatch.setattr(sd, 'load_catalog', lambda path=None: cat)
    w = suw.SourceUpdateWorker('Hessen')
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'] is False and 'noch keine Quelle eingetragen' in result['msg']


# ── URL templates with current date and data version (Hessen) ───────────────

def test_url_template_candidates_and_resolution():
    from datetime import date
    url = 'https://gds.hessen.de/downloadcenter/{datum}/HK/Hauskoordinaten-{jahr}-{nr}.zip'
    c = sd.url_template_candidates(url, today=date(2026, 10, 26))
    assert c[0] == 'https://gds.hessen.de/downloadcenter/20261026/HK/Hauskoordinaten-2026-12.zip'
    assert 'https://gds.hessen.de/downloadcenter/20261026/HK/Hauskoordinaten-2025-01.zip' in c
    assert c[24].startswith('https://gds.hessen.de/downloadcenter/20261025/')     # then yesterday
    assert len(c) == 48
    hit = 'https://gds.hessen.de/downloadcenter/20261026/HK/Hauskoordinaten-2026-01.zip'
    http = FakeHttp({hit: (206, {}, b'PK\x03\x04')})
    assert sd.resolve_url_template(url, http, today=date(2026, 10, 26)) == hit
    assert http.requests[0][1].get('Range') == 'bytes=0-3'
    with pytest.raises(sd.SourceError):
        sd.resolve_url_template(url, FakeHttp({}), today=date(2026, 10, 26))


def test_conditional_download_ignores_day_folder(tmp_path):
    a = 'https://gds.hessen.de/downloadcenter/20261025/HK/hk-2026-01.zip'
    b = 'https://gds.hessen.de/downloadcenter/20261026/HK/hk-2026-01.zip'
    http = FakeHttp({b: lambda h: (304, {}, b'') if h.get('If-None-Match') == '"e1"' else (200, {}, b'x')})
    info = sd.download_file(http, b, str(tmp_path), prev_meta={'url': a, 'etag': '"e1"'})
    assert info['status'] == 'unchanged'
