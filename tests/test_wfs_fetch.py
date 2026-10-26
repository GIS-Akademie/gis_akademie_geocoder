# -*- coding: utf-8 -*-
"""WFS fetching: paging with completeness check, BBOX tiles, loading by ID,
duplicates at tile borders — against the simulated server in fake_wfs.py."""
import os
import importlib

import pytest

from conftest import PKG_NAME
from fake_wfs import FakeWfs
sd  = importlib.import_module(f'{PKG_NAME}.source_downloader')
gml = importlib.import_module(f'{PKG_NAME}.gml_import_worker')

TYPES = ['ad:ThoroughfareName', 'ad:AdminUnitName', 'ad:Address']


def _entry(**kw):
    e = {'typ': 'wfs', 'url': 'https://wfs/x', 'format': 'gml', 'typnamen': list(TYPES),
         'seitengroesse': 10, 'max_objekte': 10}
    e.update(kw)
    return e


def _ids(files, typ):
    out = []
    for f in files:
        out += sd._feature_ids(f, typ)
    return out


def test_paging_stable_server_complete(tmp_path):
    srv = FakeWfs(n_addresses=35, n_streets=12, max_count=10)
    logs = []
    files = sd.fetch_wfs(_entry(), str(tmp_path), srv, log=logs.append)
    assert len(set(_ids(files, 'ad:Address'))) == 35
    assert len(set(_ids(files, 'ad:ThoroughfareName'))) == 12
    assert any('ad:Address: vollständig (35 Objekte)' in m for m in logs)
    assert any('ad:Address' in u and 'startIndex=35' in u for u in srv.requests)  # empty page ends the type


def test_paging_unstable_server_aborts_before_import(tmp_path):
    """Exactly the M-V case: unstable paging → abort instead of partial data."""
    srv = FakeWfs(n_addresses=60, max_count=10, unstable=True, seed=3)
    with pytest.raises(sd.SourceError, match='unvollständig'):
        sd.fetch_wfs(_entry(), str(tmp_path), srv)


def test_tiled_fetch_complete_despite_unstable_paging(tmp_path):
    srv = FakeWfs(n_addresses=60, n_streets=25, max_count=10, unstable=True, seed=3)
    e = _entry(abruf='kacheln', kachel_crs='urn:ogc:def:crs:EPSG::25833',
               kachel_bbox=[0, 0, 1000, 1000])
    # Server coordinates = grid 0..1000 (FakeWfs filters on the grid values)
    logs = []
    files = sd.fetch_wfs(e, str(tmp_path), srv, log=logs.append)
    addr = _ids(files, 'ad:Address')
    assert len(set(addr)) == 60
    assert len(addr) > 60                                        # border points twice
    # 25 streets > max_objekte 10 → fetched by ID, only the referenced ones
    assert len(set(_ids(files, 'ad:ThoroughfareName'))) == 25
    assert any('resourceId=' in u for u in srv.requests)
    assert not any('startIndex' in u and 'ThoroughfareName' in u for u in srv.requests)
    assert any('per ID geladen' in m for m in logs)
    # every loaded tile had at most max_objekte hits
    assert all(len(sd._feature_ids(f, 'ad:Address')) <= 10
               for f in files if os.path.basename(f).startswith('ad_Address'))


def test_tiled_import_dedupes_border_addresses(db, tmp_path):
    srv = FakeWfs(n_addresses=60, n_streets=25, max_count=10, unstable=True, seed=5)
    e = _entry(abruf='kacheln', kachel_crs='urn:ogc:def:crs:EPSG::25833',
               kachel_bbox=[0, 0, 1000, 1000])
    files = sd.fetch_wfs(e, str(tmp_path), srv)
    w = gml.GmlImportWorker(files, 'Mecklenburg-Vorpommern', replace=True, gemeinde_level='5thOrder')
    result, logs = {}, []
    w.detail.connect(logs.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    conn = db.get_connection()
    n, n_dist, gem = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT hausnummer), MIN(gemeinde) FROM addresses "
        "WHERE bundesland='Mecklenburg-Vorpommern'").fetchone()
    conn.close()
    assert (n, n_dist, gem) == (60, 60, 'Teststadt')
    assert any('doppelte Adressen' in m for m in logs)


def test_catalog_validation_for_tiles():
    with pytest.raises(sd.SourceError, match='kachel'):
        sd.validate_catalog({'sources': {'X': _entry(abruf='kacheln')}})
    with pytest.raises(sd.SourceError, match='abruf'):
        sd.validate_catalog({'sources': {'X': _entry(abruf='zufall')}})
    sd.validate_catalog({'sources': {'X': _entry(abruf='kacheln', kachel_crs='EPSG:25833',
                                                  kachel_bbox=[0, 0, 1, 1])}})


def test_catalog_mv_uses_tiles_hamburg_pages():
    cat = sd.load_catalog(include_user=False)
    mv, hh = cat['sources']['Mecklenburg-Vorpommern'], cat['sources']['Hamburg']
    assert (mv['abruf'], mv['kachel_crs'], mv['max_objekte']) == ('kacheln', 'urn:ogc:def:crs:EPSG::25833', 10000)
    assert hh['abruf'] == 'seiten'


def test_fetch_wfs_cancel(tmp_path):
    with pytest.raises(sd.Cancelled):
        sd.fetch_wfs(_entry(), str(tmp_path), FakeWfs(), cancel=lambda: True)


# Terms that do not belong in the end-user log window
_TECH_TERMS = ('WFS', 'Kachel', 'gml', 'GML', 'Feature', 'RESOURCEID', 'resourceId', 'numberMatched',
               'Pass ', 'Encoding', 'Traceback', 'BBOX', 'startIndex', 'quellen', 'Objekte',
               'Paging', 'ThoroughfareName', 'AdminUnitName', 'sha', 'SHA')


def test_user_log_has_no_technical_terms(db, monkeypatch, tmp_path):
    """Complete download + import of a WFS source: the log is readable for
    end users, the technical details go to the detail channel (detail)."""
    suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')
    srv = FakeWfs(n_addresses=60, n_streets=25, max_count=10, unstable=True, seed=7)
    entry = _entry(abruf='kacheln', kachel_crs='urn:ogc:def:crs:EPSG::25833',
                   kachel_bbox=[0, 0, 1000, 1000], gemeinde_ebene='5thOrder',
                   quelle_name='Testamt – Adressen', lizenz='dl-de/by-2-0', dauer='etwa 1 Minute',
                   hinweis='Ohne Postleitzahlen.', technik='WFS mit instabilem Paging, Kacheln')
    cat = {'version': 1, 'user_agent': 'T/{version}', 'sources': {'Mecklenburg-Vorpommern': entry}}
    monkeypatch.setattr(sd, 'load_catalog', lambda *a, **k: cat)
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: srv)

    w = suw.SourceUpdateWorker('Mecklenburg-Vorpommern', plugin_version='t')
    logs, details, progress, result = [], [], [], {}
    w.log.connect(logs.append)
    w.detail.connect(details.append)
    w.progress.connect(lambda d, t, txt: progress.append(txt))
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()

    assert result == {'ok': True, 'msg': 'Mecklenburg-Vorpommern: 60 Adressen importiert.'}
    user_text = '\n'.join(logs + progress + [result['msg']])
    found = [t for t in _TECH_TERMS if t in user_text]
    assert not found, f'Technische Begriffe im Protokoll: {found}\n{user_text}'
    assert 'Quelle: Testamt – Adressen · Lizenz dl-de/by-2-0' in logs
    assert any('das dauert etwa 1 Minute' in m for m in logs)
    assert any('Adressen geladen' in t for t in progress)
    # the technical details are not lost but in the detail channel
    detail_text = '\n'.join(details)
    assert 'vollständig' in detail_text and 'ad:Address' in detail_text
    assert 'doppelte Adressen' in detail_text


def test_user_error_message_is_readable(db, monkeypatch):
    """Unstable paging without tiles: understandable message, technical details in detail."""
    suw = importlib.import_module(f'{PKG_NAME}.source_update_worker')
    srv = FakeWfs(n_addresses=60, max_count=10, unstable=True, seed=3)
    cat = {'version': 1, 'sources': {'Hamburg': _entry(quelle_name='Test', lizenz='x')}}
    monkeypatch.setattr(sd, 'load_catalog', lambda *a, **k: cat)
    monkeypatch.setattr(sd, 'Http', lambda ua, timeout=60: srv)
    w = suw.SourceUpdateWorker('Hamburg')
    details, result = [], {}
    w.detail.connect(details.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'] is False
    assert result['msg'].startswith('Der Download war unvollständig (')
    assert 'Der bisherige Datenbestand bleibt erhalten' in result['msg']
    assert not [t for t in ('numberMatched', 'abruf', 'Paging') if t in result['msg']]
    assert any('numberMatched' in d for d in details)


def test_http_errors_are_readable():
    e = sd._http_error(503, 'https://www.geodaten-mv.de/dienste/x?a=1')
    assert str(e) == ('Der Server www.geodaten-mv.de ist derzeit gestört (Fehler 503). '
                      'Bitte später erneut versuchen.')
    assert 'https://www.geodaten-mv.de/dienste/x?a=1' in e.detail
    e = sd._net_error('https://gdi.berlin.de/x', 'timed out')
    assert str(e).startswith('Keine Verbindung zum Server gdi.berlin.de.')
