# -*- coding: utf-8 -*-
"""Source list delivered online (sources.json from the repository):
fetching, validation, precedence over the bundled list, protection against
invalid or dangerous entries — without network."""
import json
import importlib

import pytest

from conftest import PKG_NAME
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
from test_source_downloader import FakeHttp   # noqa: E402

URL = 'https://example.org/sources.json'


@pytest.fixture
def online(tmp_path):
    old = sd.ONLINE_CATALOG_PATH
    sd.set_online_catalog_path(str(tmp_path / 'profil' / 'sources_online.json'))
    yield sd.ONLINE_CATALOG_PATH
    sd.set_online_catalog_path(old)


def _base():
    return sd.load_catalog(include_user=False, include_online=False)


def _online(stand, sources=None, version=1, **extra):
    base = _base()
    src = {bl: {k: v for k, v in e.items() if not k.startswith('_')} for bl, e in base['sources'].items()}
    src.update(sources or {})
    d = {'version': version, 'stand': stand, 'sources': src}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False).encode('utf-8')


def test_bundled_catalog_has_stand_and_url():
    base = _base()
    assert base['stand'] >= '2026-10-26' and base['_quelle'] == 'mitgeliefert'
    assert base['katalog_url'] == sd.DEFAULT_CATALOG_URL
    assert base['katalog_url'].startswith('https://raw.githubusercontent.com/')


def test_newer_online_catalog_overrides_entry(online):
    neu_hessen = {'typ': 'direct', 'url': 'https://gds.hessen.de/hk_he.zip', 'format': 'hkde',
                  'quelle_name': 'HVBG – Direktlink', 'lizenz': 'dl-de/by-2-0'}
    http = FakeHttp({URL: (200, {'etag': '"v2"'}, _online('2099-01-01', {'Hessen': neu_hessen}))})
    status, text = sd.fetch_online_catalog(http=http, url=URL)
    assert status == 'neu' and '01.01.2099' in text
    cat = sd.load_catalog()
    assert cat['_quelle'] == 'online' and cat['stand'] == '2099-01-01'
    assert cat['sources']['Hessen']['typ'] == 'direct' and cat['sources']['Hessen']['_herkunft'] == 'standard'
    assert 'Hessen' in sd.registered_bundeslaender(cat)

    # Second fetch: conditional via ETag, server answers 304
    http.responses[URL] = lambda h: (304, {}, b'') if h.get('If-None-Match') == '"v2"' else (500, {}, b'')
    status, text = sd.fetch_online_catalog(http=http, url=URL)
    assert status == 'aktuell' and 'aktuell' in text
    assert sd.load_catalog()['_quelle'] == 'online'


def test_older_online_catalog_is_ignored(online):
    http = FakeHttp({URL: _online('2000-01-01', {'Thüringen': {
        'typ': 'direct', 'url': 'https://x/alt.zip', 'format': 'hkde'}})})
    status, _ = sd.fetch_online_catalog(http=http, url=URL)
    assert status == 'aktuell'
    cat = sd.load_catalog()
    assert cat['_quelle'] == 'mitgeliefert'
    assert cat['sources']['Thüringen']['url'] == _base()['sources']['Thüringen']['url']


def test_user_source_still_wins(online, tmp_path):
    old = sd.USER_CATALOG_PATH
    sd.set_user_catalog_path(str(tmp_path / 'profil' / 'sources_user.json'))
    try:
        sd.save_user_source('Hessen', {'typ': 'datei', 'pfad': 'C:/daten/hk.txt', 'format': 'hkde'})
        http = FakeHttp({URL: _online('2099-01-01', {'Hessen': {
            'typ': 'direct', 'url': 'https://x/he.zip', 'format': 'hkde'}})})
        sd.fetch_online_catalog(http=http, url=URL)
        cat = sd.load_catalog()
        assert cat['sources']['Hessen']['typ'] == 'datei' and cat['sources']['Hessen']['_herkunft'] == 'eigene'
    finally:
        sd.set_user_catalog_path(old)


def test_dangerous_or_unknown_entries_are_skipped(online):
    http = FakeHttp({URL: _online('2099-01-01', {
        'Hessen':   {'typ': 'datei', 'pfad': 'C:/Windows/system32/x.txt', 'format': 'hkde', 'url': 'x'},
        'Saarland': {'typ': 'direct', 'url': 'file:///C:/geheim.txt', 'format': 'hkde'},
        'Thüringen': {'typ': 'zukunftstyp', 'url': 'https://x', 'format': 'hkde'},
        'Bremen':   {'typ': 'direct', 'url': 'https://x/b.zip', 'format': 'parquet'},
    })})
    assert sd.fetch_online_catalog(http=http, url=URL)[0] == 'neu'
    cat = sd.load_catalog()
    base = _base()
    for bl in ('Hessen', 'Saarland', 'Thüringen', 'Bremen'):            # bundled entry is kept
        assert cat['sources'][bl]['typ'] == base['sources'][bl]['typ'], bl
        assert cat['sources'][bl].get('url') == base['sources'][bl].get('url'), bl
    assert cat['_quelle'] == 'online'                                      # remaining entries apply


def test_newer_format_is_rejected(online):
    http = FakeHttp({URL: _online('2099-01-01', version=sd.CATALOG_SCHEMA + 1)})
    status, text = sd.fetch_online_catalog(http=http, url=URL)
    assert status == 'zu_neu' and 'neuere Version des Plugins' in text
    assert sd.load_catalog()['_quelle'] == 'mitgeliefert'


def test_invalid_or_unreachable(online):
    http = FakeHttp({URL: b'<html>Fehler</html>'})
    assert sd.fetch_online_catalog(http=http, url=URL)[0] == 'fehler'
    status, text = sd.fetch_online_catalog(http=FakeHttp({}), url=URL)    # 404
    assert status == 'fehler' and 'bisherige' in text
    assert sd.load_catalog()['_quelle'] == 'mitgeliefert'


def test_online_catalog_can_move_its_url(online):
    neu = 'https://example.org/neu/sources.json'
    http = FakeHttp({URL: _online('2099-01-01', katalog_url=neu),
                     neu: (200, {}, _online('2099-02-01'))})
    sd.fetch_online_catalog(http=http, url=URL)
    assert sd.load_catalog()['katalog_url'] == neu
    status, _ = sd.fetch_online_catalog(http=http)                        # uses the new address
    assert status == 'neu' and http.requests[-1][0] == neu


def test_without_storage_path():
    old = sd.ONLINE_CATALOG_PATH
    sd.set_online_catalog_path(None)
    try:
        assert sd.fetch_online_catalog(http=FakeHttp({}), url=URL)[0] == 'fehler'
    finally:
        sd.set_online_catalog_path(old)
