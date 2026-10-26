# -*- coding: utf-8 -*-
"""Robustness of the download layer (review fixes): network errors during the
response, truncated downloads, template probing, WFS servers that ignore
startIndex, file name collisions, online list protection, lock after a
download only for the same source — without internet (loopback servers only)."""
import io
import json
import socket
import zipfile
import hashlib
import importlib
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import PKG_NAME
sd = importlib.import_module(f'{PKG_NAME}.source_downloader')
from test_source_downloader import FakeHttp   # noqa: E402


# ── Loopback servers ─────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    mode = 'ok'

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.mode == 'truncated':
            self.send_response(200)
            self.send_header('Content-Length', '1000')
            self.end_headers()
            self.wfile.write(b'x' * 100)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        elif self.mode == 'html':
            body = b'<html>Wartung</html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b'PK-data'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture
def server():
    srv = HTTPServer(('127.0.0.1', 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def silent_server():
    """Accepts connections but never answers (a server that stalls)."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen(5)
    conns = []
    stop = threading.Event()

    def accept():
        s.settimeout(0.2)
        while not stop.is_set():
            try:
                conns.append(s.accept()[0])
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield s.getsockname()[1]
    stop.set()
    for c in conns:
        c.close()
    s.close()


def _url(srv, path='/f.zip'):
    return f'http://127.0.0.1:{srv.server_address[1]}{path}'


def test_truncated_download_is_an_error(server, tmp_path):
    _Handler.mode = 'truncated'
    http = sd.Http('test', timeout=5)
    with pytest.raises(sd.SourceError) as e:
        http.download(_url(server), str(tmp_path / 'f.zip'))
    assert e.value.kind == 'voruebergehend' and 'vorzeitig' in str(e.value)
    assert not (tmp_path / 'f.zip').exists() and not (tmp_path / 'f.zip.part').exists()


def test_complete_download_ok(server, tmp_path):
    _Handler.mode = 'ok'
    status, hdrs = sd.Http('test', timeout=5).download(_url(server), str(tmp_path / 'f.zip'))
    assert status == 200 and (tmp_path / 'f.zip').read_bytes() == b'PK-data'
    assert hdrs['_sha256'] == hashlib.sha256(b'PK-data').hexdigest()


def test_stalled_server_raises_source_error(silent_server, tmp_path):
    http = sd.Http('test', timeout=0.5)
    url = f'http://127.0.0.1:{silent_server}/x'
    for call in (lambda: http.get(url), lambda: http.download(url, str(tmp_path / 'x'))):
        with pytest.raises(sd.SourceError) as e:
            call()
        assert e.value.kind == 'voruebergehend'


def test_html_page_is_not_a_template_candidate(server):
    _Handler.mode = 'html'
    with pytest.raises(sd.SourceError) as e:
        sd.resolve_url_template(_url(server, '/hk-{jahr}-{nr}.zip'), sd.Http('t', timeout=5),
                                today=date(2026, 10, 26))
    assert e.value.kind == 'geaendert'


def test_template_probe_stops_on_network_problems():
    class H(FakeHttp):
        def get(self, url, headers=None, max_bytes=None):
            self.requests.append(url)
            raise sd.SourceError('weg', kind='voruebergehend')
    h = H({})
    with pytest.raises(sd.SourceError):
        sd.resolve_url_template('https://x/hk-{jahr}-{nr}.zip', h, today=date(2026, 10, 26))
    assert len(h.requests) == 1                   # not 24 candidates against a dead server


# ── WFS: server ignores startIndex (Brandenburg) ─────────────────────────────

_NS = 'xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" ' \
      'xmlns:ad="urn:x-inspire:specification:gmlas:Addresses:3.0"'


class _IgnoresStartIndex:
    def __init__(self, n=3):
        self.items = [f'<wfs:member><ad:AdminUnitName gml:id="AdminUnitName_{i}"/></wfs:member>'
                      for i in range(n)]
        self.pages = 0

    def get(self, url, headers=None, max_bytes=None):
        return 200, {}, f'<wfs:FeatureCollection {_NS} numberMatched="{len(self.items)}"/>'.encode()

    def download(self, url, dest, headers=None, progress=None, cancel=None):
        self.pages += 1
        assert self.pages < 10, 'endless paging'
        body = f'<wfs:FeatureCollection {_NS}>{"".join(self.items)}</wfs:FeatureCollection>'.encode()
        with open(dest, 'wb') as f:
            f.write(body)
        return 200, {'_sha256': hashlib.sha256(body).hexdigest()}


def test_paging_ends_when_server_repeats_the_first_page(tmp_path):
    srv = _IgnoresStartIndex(3)
    entry = {'url': 'https://wfs', 'typnamen': ['ad:AdminUnitName'], 'seitengroesse': 100}
    shas = []
    files = sd.fetch_wfs(entry, str(tmp_path), srv, shas=shas)
    assert len(files) == 1 and srv.pages == 2 and len(shas) == 1


# ── File names: no overwriting ───────────────────────────────────────────────

def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_zip_members_with_same_name_do_not_overwrite(tmp_path):
    arc = tmp_path / 'a.zip'
    arc.write_bytes(_zip({'Kreis_A/hk.txt': 'A', 'Kreis_B/hk.txt': 'B'}))
    out = tmp_path / 'out'
    out.mkdir()
    files = sd.extract_data_files(str(arc), str(out), r'\.txt$')
    assert len(set(files)) == 2
    assert sorted(open(f, encoding='utf-8').read() for f in files) == ['A', 'B']


def test_several_urls_get_distinct_file_names(tmp_path):
    http = FakeHttp({'https://x/dl?id=1': b'eins', 'https://x/dl?id=2': b'zwei',
                     'https://x/seite': b'<a href="dl?id=1">1</a> <a href="dl?id=2">2</a>'})
    cat = {'sources': {'Testland': {'typ': 'page_link', 'url': 'https://x/seite', 'format': 'hkde',
                                    'link_muster': r'href="(dl\?id=\d)"'}}}
    r = sd.update_source('Testland', str(tmp_path / 'g.db'), catalog=cat, http=http)
    assert sorted(open(f, encoding='utf-8').read() for f in r['files']) == ['eins', 'zwei']


def test_links_from_html_are_unescaped():
    assert sd._absolute('https://x/seite', 'dl?id=1&amp;fmt=zip') == 'https://x/dl?id=1&fmt=zip'


# ── Lock after a download: only for the same source ──────────────────────────

def test_lock_applies_only_to_the_same_source(tmp_path):
    cat = {'sources': {'Bayern': {'typ': 'direct', 'url': 'https://neu/b.osm.pbf', 'format': 'osm',
                                  'min_abstand_tage': 28}}}
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    http = FakeHttp({'https://neu/b.osm.pbf': b'osm'})
    other = {'url': 'https://alt/b.osm.pbf', 'heruntergeladen_am': recent}
    r = sd.update_source('Bayern', str(tmp_path / 'g.db'), prev_meta=other, catalog=cat, http=http)
    assert r['status'] == 'downloaded'
    same = {'url': 'https://neu/b.osm.pbf', 'heruntergeladen_am': recent}
    r = sd.update_source('Bayern', str(tmp_path / 'g.db'), prev_meta=same, catalog=cat, http=http)
    assert r['status'] == 'skipped'


def test_lock_matches_template_urls():
    entry = {'url': 'https://x/{datum}/hk-{jahr}-{nr}.zip'}
    assert sd._same_source({'url': 'https://x/20261026/hk-2026-01.zip'}, entry)
    assert not sd._same_source({'url': 'https://y/20261026/hk-2026-01.zip'}, entry)


# ── Online list ──────────────────────────────────────────────────────────────

@pytest.fixture
def online(tmp_path):
    old = sd.ONLINE_CATALOG_PATH
    sd.set_online_catalog_path(str(tmp_path / 'sources_online.json'))
    yield sd.ONLINE_CATALOG_PATH
    sd.set_online_catalog_path(old)


def _liste(stand, version=1):
    base = sd.load_catalog(include_user=False, include_online=False)
    data = {k: v for k, v in base.items() if not k.startswith('_')}
    data['sources'] = {bl: {k: v for k, v in e.items() if not k.startswith('_')}
                       for bl, e in base['sources'].items()}
    data['stand'], data['version'] = stand, version
    return json.dumps(data).encode()


def test_older_online_list_does_not_replace_a_newer_cached_one(online):
    url = 'https://example.org/sources.json'
    sd.fetch_online_catalog(http=FakeHttp({url: _liste('2099-03-01')}), url=url)
    status, _ = sd.fetch_online_catalog(http=FakeHttp({url: _liste('2099-02-01')}), url=url)
    assert status == 'aktuell'
    assert json.load(open(online, encoding='utf-8'))['stand'] == '2099-03-01'


def test_online_list_version_as_text(online):
    url = 'https://example.org/sources.json'
    assert sd.fetch_online_catalog(http=FakeHttp({url: _liste('2099-03-01', '1.0')}), url=url)[0] == 'neu'


def test_online_list_never_raises(online):
    class Boom:
        def get(self, *a, **k):
            raise TimeoutError('timed out')
    assert sd.fetch_online_catalog(http=Boom(), url='https://x')[0] == 'fehler'


def test_online_entry_checks_fallback_url_and_formats():
    good = {'typ': 'direct', 'url': 'https://x/a.zip', 'format': 'hkde'}
    sd._valid_online_entry('X', good)
    with pytest.raises(sd.SourceError):
        sd._valid_online_entry('X', dict(good, ausweich={'typ': 'direct', 'format': 'hkde',
                                                         'url': 'file:///C:/Windows/win.ini'}))
    with pytest.raises(sd.SourceError):
        sd._valid_online_entry('X', dict(good, format='alkis'))      # direct cannot deliver ALKIS


# ── "Quelle prüfen" ──────────────────────────────────────────────────────────

def test_test_source_ogcapi():
    entry = {'typ': 'ogcapi', 'url': 'https://ogc/items', 'format': 'geojson',
             'felder': {'strasse': 'STN', 'hausnummer': 'HNR'}}

    class H(FakeHttp):
        def get(self, url, headers=None, max_bytes=None):
            assert 'limit=1' in url
            return 200, {}, json.dumps({'features': [{}], 'numberMatched': 330251}).encode()
    ok, txt = sd.test_source(entry, http=H({}))
    assert ok and '330.251' in txt


def test_test_source_never_raises():
    class Boom:
        def get(self, *a, **k):
            raise TimeoutError('timed out')
    ok, txt = sd.test_source({'typ': 'gaialight', 'url': 'https://x', 'format': 'hkde',
                              'objekt': 'hk'}, http=Boom())
    assert not ok and 'TimeoutError' in txt
