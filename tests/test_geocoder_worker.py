# -*- coding: utf-8 -*-
"""Tests for geocoder_worker.py without QGIS (Qt stub from conftest.py).
Nominatim is never contacted: all runs in GDPR mode (local_only)."""
import pytest

from conftest import gw


COL_MAP = {'strasse': 'str', 'hausnummer': 'hn', 'plz': 'plz', 'stadt': 'ort', 'land': 'land'}


def _run(worker_mod, rows, col_map=COL_MAP):
    bls = list(worker_mod.get_db_stats().keys())
    w = worker_mod.GeocoderWorker(rows, col_map, local_only=True, bundeslaender=bls)
    out = []
    w.result_ready.connect(lambda res: out.extend(res))
    w.run()
    return out


def test_plugin_version_read_from_metadata():
    assert gw.PLUGIN_VERSION[0].isdigit()


@pytest.mark.parametrize('text, expected', [
    ('Lorettoplatz 7, 81377 München', {'strasse': 'Lorettoplatz 7', 'plz': '81377', 'stadt': 'München'}),
    ('Elchweg 3, Bochum', {'strasse': 'Elchweg 3', 'plz': '', 'stadt': 'Bochum'}),
    ('44869 Bochum, Elchweg 3', {'strasse': 'Elchweg 3', 'plz': '44869', 'stadt': 'Bochum'}),
    ('Elchweg 3 44869 Bochum', {'strasse': 'Elchweg 3', 'plz': '44869', 'stadt': 'Bochum'}),
    ('Nur Straße 5', {'strasse': 'Nur Straße 5', 'plz': '', 'stadt': ''}),
])
def test_parse_address_line(text, expected):
    parts = gw._parse_address_line(text)
    assert {k: parts[k] for k in expected} == expected
    assert 'hausnummer' in parts and 'land' in parts


def test_format_address():
    f = gw.GeocoderWorker._format_address
    assert f({'strasse': 'Elchweg', 'hausnummer': '3', 'plz': '44869', 'stadt': 'Bochum', 'land': ''}) \
        == 'Elchweg 3, 44869, Bochum'
    assert f({'strasse': 'Elchweg 3', 'hausnummer': '', 'plz': '', 'stadt': 'Bochum', 'land': 'NRW'}) \
        == 'Elchweg 3, Bochum, NRW'


def test_detect_bundesland(db):
    bls = list(db.get_db_stats().keys())
    d = gw._detect_bundesland
    assert d({'land': 'NRW', 'plz': '', 'stadt': 'Bochum'}, bls) == ('Nordrhein-Westfalen', True)
    assert d({'land': 'Nordrhein Westfalen', 'plz': '', 'stadt': ''}, bls) == ('Nordrhein-Westfalen', True)
    assert d({'land': 'Deutschland', 'plz': '44869', 'stadt': ''}, bls) == ('Nordrhein-Westfalen', True)
    assert d({'land': 'Nordrein-Westfalen', 'plz': '44869', 'stadt': ''}, bls) == ('Nordrhein-Westfalen', True)
    assert d({'land': 'Saarland', 'plz': '', 'stadt': 'Saarbrücken'}, bls) == (None, False)   # not imported
    assert d({'land': '', 'plz': '37073', 'stadt': 'Göttingen'}, bls) == (None, True)       # ambiguous prefix
    assert d({'land': '', 'plz': '', 'stadt': ''}, []) == (None, False)


def test_no_locality_skips_local_search(worker_mod):
    out = _run(worker_mod, [{'str': 'Elchweg 3', 'hn': '', 'plz': '', 'ort': '', 'land': ''}])
    r = out[0]
    assert r['geocoded'] is False
    assert r['source'] == 'Lokal (kein Treffer)'
    assert r['local_diagnose'] == 'Keine PLZ und kein Ort angegeben – lokale Suche nicht möglich'


def test_exact_hit_level_5(worker_mod):
    out = _run(worker_mod, [{'str': 'Elchweg', 'hn': '3', 'plz': '44869', 'ort': 'Bochum', 'land': 'NRW'}])
    r = out[0]
    assert (r['geocoded'], r['accuracy_level'], r['accuracy_label'], r['name']) == (True, 5, 'Hausnummer', 'Elchweg 3')
    assert r['source'] == 'Lokal (Nordrhein-Westfalen)'
    assert r['epsg'] == 25832 and r['utm_x'] is not None
    assert r['address_query'] == 'Elchweg 3, 44869, Bochum, NRW'


def test_missing_hausnummer_uses_nearest(worker_mod):
    out = _run(worker_mod, [{'str': 'Lorettoplatz 999', 'hn': '', 'plz': '81377', 'ort': 'München', 'land': ''}])
    r = out[0]
    assert (r['accuracy_level'], r['accuracy_label'], r['name']) == (4, 'Straße (nächste Hausnummer)', 'Lorettoplatz 7')


def test_no_hausnummer_uses_center(worker_mod):
    out = _run(worker_mod, [{'str': 'Lorettoplatz', 'hn': '', 'plz': '81377', 'ort': 'München', 'land': ''}])
    r = out[0]
    assert (r['accuracy_level'], r['accuracy_label'], r['name']) == (4, 'Straße (Mittelpunkt)', 'Lorettoplatz')


def test_numbered_street_with_separate_hausnummer(worker_mod):
    out = _run(worker_mod, [{'str': 'Straße 10', 'hn': '5', 'plz': '', 'ort': 'Berlin', 'land': ''}])
    assert out[0]['name'] == 'Straße 10 5' and out[0]['accuracy_level'] == 5


def test_range_hausnummer(worker_mod):
    out = _run(worker_mod, [{'str': 'Elchweg', 'hn': '3-5', 'plz': '', 'ort': 'Bochum', 'land': ''}])
    assert out[0]['name'] == 'Elchweg 3' and out[0]['accuracy_level'] == 5


def test_ambiguous_city_hint_in_label(worker_mod):
    out = _run(worker_mod, [{'str': 'Zeil 2', 'hn': '', 'plz': '', 'ort': 'Frankfurt', 'land': ''}])
    r = out[0]
    assert r['matched_bundesland'] == 'Hessen'
    assert 'Ort mehrdeutig' in r['local_score_label']


def test_locality_fallback_level_2(worker_mod):
    out = _run(worker_mod, [{'str': 'Gibtsnichtweg 1', 'hn': '', 'plz': '', 'ort': 'Pankow', 'land': ''}])
    r = out[0]
    assert (r['geocoded'], r['accuracy_level'], r['accuracy_label']) == (True, 2, 'Ort/Gemeinde (Schwerpunkt)')
    assert r['matched_bundesland'] == 'Berlin'


def test_single_column_mode(worker_mod):
    out = _run(worker_mod, [{'adr': 'Elchweg 3, 44869 Bochum'}], col_map={'combined': 'adr'})
    assert out[0]['accuracy_level'] == 5 and out[0]['name'] == 'Elchweg 3'


def test_cancel_stops_early(worker_mod):
    rows = [{'str': 'Elchweg', 'hn': '3', 'plz': '44869', 'ort': 'Bochum', 'land': ''}] * 5
    bls = list(worker_mod.get_db_stats().keys())
    w = worker_mod.GeocoderWorker(rows, COL_MAP, local_only=True, bundeslaender=bls)
    out = []
    w.result_ready.connect(lambda res: out.extend(res))
    w.progress.connect(lambda i, n, a: w.cancel() if i == 2 else None)
    w.run()
    assert len(out) == 2


# ── Robustness: locked database / errors (review A10) ────────────────────────

def _collect(w):
    got = {'results': None, 'finished': False, 'progress': []}
    w.result_ready.connect(lambda r: got.__setitem__('results', r))
    w.finished.connect(lambda: got.__setitem__('finished', True))
    w.progress.connect(lambda i, n, t: got['progress'].append(t))
    return got


def test_locked_db_at_start_reports_and_finishes(worker_mod, monkeypatch):
    monkeypatch.setattr(gw, 'db_state', lambda timeout=5.0: 'locked')
    rows = [{'str': 'Elchweg', 'hn': '3', 'plz': '44869', 'ort': 'Bochum', 'land': ''}]
    w = gw.GeocoderWorker(rows, COL_MAP, local_only=True, bundeslaender=['Nordrhein-Westfalen'])
    got = _collect(w)
    w.run()
    assert got['finished'] and got['results'] == []
    assert 'Datenbank-Import' in w.abort_reason and 'gesperrt' in got['progress'][-1]


def test_locked_db_mid_run_keeps_results(worker_mod, monkeypatch):
    import sqlite3
    from conftest import lg
    # keep the one-time checks out of the way (they would wait for the lock)
    monkeypatch.setattr(lg, 'ensure_search_indexes', lambda log=None: False)
    monkeypatch.setattr(gw, 'ensure_lookup_tables', lambda **k: False)
    monkeypatch.setattr(gw, 'get_connection', lambda timeout=5.0: lg.get_connection(0.05))
    rows = [{'str': 'Elchweg', 'hn': '3', 'plz': '44869', 'ort': 'Bochum', 'land': ''}] * 3
    w = gw.GeocoderWorker(rows, COL_MAP, local_only=True, bundeslaender=['Nordrhein-Westfalen'])
    got = _collect(w)
    writer = sqlite3.connect(lg.DB_PATH)

    def lock_after_first(i, n, text):
        if i == 2 and not writer.in_transaction:
            writer.execute('BEGIN EXCLUSIVE')       # an import starts writing
    w.progress.connect(lock_after_first)
    try:
        w.run()
    finally:
        writer.rollback()
        writer.close()
    assert got['finished']
    assert len(got['results']) == 1 and got['results'][0]['accuracy_level'] == 5
    assert w.abort_reason.startswith('Die lokale Datenbank ist gesperrt')
    assert '(1 von 3)' in w.abort_reason


def test_unexpected_error_still_finishes(worker_mod, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('kaputt')
    monkeypatch.setattr(gw, 'local_search', boom)
    rows = [{'str': 'Elchweg', 'hn': '3', 'plz': '44869', 'ort': 'Bochum', 'land': ''}]
    w = gw.GeocoderWorker(rows, COL_MAP, local_only=True, bundeslaender=['Nordrhein-Westfalen'])
    got = _collect(w)
    details = []
    w.detail.connect(details.append)
    w.run()
    assert got['finished'] and got['results'] == []
    assert 'RuntimeError' in w.abort_reason and details and 'kaputt' in details[0]


# ── PLZ / house number handling in the worker ────────────────────────────────

def test_numeric_plz_from_spreadsheet(worker_mod):
    out = _run(worker_mod, [{'str': 'Elchweg', 'hn': '3', 'plz': '44869.0', 'ort': '', 'land': ''}])
    assert out[0]['accuracy_level'] == 5 and out[0]['address_query'] == 'Elchweg 3, 44869'


def test_glued_hausnummer_in_worker(worker_mod):
    out = _run(worker_mod, [{'str': 'Elchweg3', 'hn': '', 'plz': '44869', 'ort': 'Bochum', 'land': ''}])
    assert (out[0]['accuracy_level'], out[0]['name']) == (5, 'Elchweg 3')


# ── Nominatim (B3, B5) — urlopen is replaced, nothing goes online ────────────

def test_importance_does_not_raise_level():
    assert gw.calc_accuracy_nominatim('road', 'residential', 'highway', 0.9) == (4, 'Straße')
    assert gw.calc_accuracy_nominatim('city', 'city', 'place', 0.8) == (2, 'Stadt')


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        import json
        return json.dumps(self._data).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_nominatim_429_backoff_and_countrycodes(monkeypatch):
    import urllib.error
    calls, sleeps = [], []

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, None)
        return _Resp([{'lat': '52.5', 'lon': '13.4', 'addresstype': 'road', 'type': 'residential',
                       'category': 'highway', 'importance': 0.9, 'name': 'Zeil'}])
    monkeypatch.setattr(gw.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr(gw.time, 'sleep', lambda s: sleeps.append(s))
    w = gw.GeocoderWorker([], {}, delay=1.1)
    res = w._geocode_nominatim('Zeil 1, Frankfurt')
    assert res['geocoded'] and res['accuracy_level'] == 4
    assert len(calls) == 3 and all('countrycodes=de' in u for u in calls)
    assert sum(sleeps) == pytest.approx(gw.NOMINATIM_RETRY_DELAYS[0] + gw.NOMINATIM_RETRY_DELAYS[1])
    assert w.delay > 1.1                       # following rows are slowed down


def test_nominatim_429_gives_up_after_retries(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, None)
    monkeypatch.setattr(gw.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr(gw.time, 'sleep', lambda s: None)
    w = gw.GeocoderWorker([], {})
    res = w._geocode_nominatim('x')
    assert not res['geocoded'] and 'zu viele Anfragen' in res['accuracy_label']
