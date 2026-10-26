# -*- coding: utf-8 -*-
"""Import workers: one transaction for several files, shrink guard, encoding and
format detection, municipality names, house numbers, GML details (srsName, PLZ,
broken files), PLZ post-processing (cancel, streaming, cache key) and the
shared HK-BY row builder."""
import os
import sqlite3
import importlib

import pytest

from conftest import lg, pr, PKG_NAME
from test_source_downloader import _hkde_line
from test_gml_import import _name, _address, _collection
from test_plz_gebiete import _write_areas, _square, areas

dbi = importlib.import_module(f'{PKG_NAME}.db_import_worker')
gml = importlib.import_module(f'{PKG_NAME}.gml_import_worker')
oc  = importlib.import_module(f'{PKG_NAME}.osm_convert')

NRW = 'Nordrhein-Westfalen'
_FIXTURES = (areas,)     # PLZ test areas (fixture from test_plz_gebiete)


def _run(worker):
    result, logs = {}, []
    worker.log.connect(logs.append)
    worker.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    worker.run()
    return result, logs


def _write(path, lines, encoding='utf-8'):
    with open(path, 'w', encoding=encoding, newline='') as f:
        f.write('\r\n'.join(lines) + '\r\n')
    return str(path)


def _rows(db, bl):
    conn = db.get_connection()
    rows = conn.execute("SELECT gemeinde, strasse, hausnummer, hausnr_zusatz, plz FROM addresses "
                        "WHERE bundesland = ? ORDER BY strasse, hausnummer", (bl,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


# ── A1: several files in ONE transaction ─────────────────────────────────────

def test_multi_file_import_is_one_transaction(db, tmp_path):
    before = _rows(db, NRW)
    f1 = _write(tmp_path / 'a.txt', [_hkde_line('Bochum', 'Neuweg', '1', 371000.0, 5701000.0)])
    f2 = _write(tmp_path / 'b.txt', [_hkde_line('Bochum', 'Neuweg', '2', 371010.0, 5701010.0)])
    res, _ = _run(dbi.DbImportWorker([f1, f2], NRW, replace=True))
    assert res['ok'], res
    assert _rows(db, NRW) == [('Bochum', 'Neuweg', '1', '', ''), ('Bochum', 'Neuweg', '2', '', '')]
    assert before != _rows(db, NRW)


def test_multi_file_second_file_empty_keeps_state(db, tmp_path):
    before = _rows(db, NRW)
    f1 = _write(tmp_path / 'a.txt', [_hkde_line('Bochum', 'Neuweg', '1', 371000.0, 5701000.0)])
    f2 = _write(tmp_path / 'liesmich.txt', ['Diese Datei enthält keine Adressen.'])
    res, _ = _run(dbi.DbImportWorker([f1, f2], NRW, replace=True))
    assert res['ok'] is False and 'liesmich.txt' in res['msg'] and 'bleibt erhalten' in res['msg']
    assert _rows(db, NRW) == before


def test_multi_file_cancel_keeps_state(db, tmp_path):
    before = _rows(db, NRW)
    f1 = _write(tmp_path / 'a.txt', [_hkde_line('Bochum', 'Neuweg', '1', 371000.0, 5701000.0)])
    f2 = _write(tmp_path / 'b.txt', [_hkde_line('Bochum', 'Neuweg', '2', 371010.0, 5701010.0)])
    w = dbi.DbImportWorker([f1, f2], NRW, replace=True)
    w.log.connect(lambda m: w.cancel() if 'b.txt' in m else None)
    res, _ = _run(w)
    assert res == {'ok': False, 'msg': dbi.CANCEL_MSG} and 'abgebrochen' in res['msg']
    assert _rows(db, NRW) == before


def test_single_file_str_still_accepted(db, tmp_path):
    f = _write(tmp_path / 'a.txt', [_hkde_line('Bochum', 'Neuweg', '1', 371000.0, 5701000.0)])
    w = dbi.DbImportWorker(f, NRW)
    assert w.filepaths == [f] and w.filepath == f
    assert _run(w)[0]['ok']


# ── B50: shrink guard ────────────────────────────────────────────────────────

def _fill(db, bl, n):
    conn = sqlite3.connect(db.DB_PATH)
    conn.executemany("INSERT INTO addresses (bundesland, gemeinde, plz, strasse, hausnummer, epsg, x, y, "
                     "strasse_norm, gemeinde_norm) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [(bl, 'Bremen', '28195', 'Alt', str(i), 25832, 487000.0, 5885000.0, 'alt', 'bremen')
                      for i in range(n)])
    conn.commit()
    conn.close()


def _count(db, bl):
    conn = sqlite3.connect(db.DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland = ?", (bl,)).fetchone()[0]
    conn.close()
    return n


def test_shrink_guard_rolls_back_and_allow_shrink_accepts(db, tmp_path):
    _fill(db, 'Bremen', 1500)
    f = _write(tmp_path / 'hb.txt', [_hkde_line('Bremen', 'Neu', str(i), 487000.0 + i, 5885000.0)
                                     for i in range(1, 11)])
    res, _ = _run(dbi.DbImportWorker(f, 'Bremen', replace=True))
    assert res['ok'] is False
    assert res['msg'] == dbi.shrink_msg(10, 1500)
    assert 'nur 10 statt bisher 1.500 Adressen' in res['msg'] and 'bleibt erhalten' in res['msg']
    assert _count(db, 'Bremen') == 1500

    res, _ = _run(dbi.DbImportWorker(f, 'Bremen', replace=True, allow_shrink=True))
    assert res['ok'], res
    assert _count(db, 'Bremen') == 10


def test_shrink_guard_ignores_small_states_and_moderate_decline(db, tmp_path):
    _fill(db, 'Bremen', 999)
    f = _write(tmp_path / 'hb.txt', [_hkde_line('Bremen', 'Neu', '1', 487000.0, 5885000.0)])
    assert _run(dbi.DbImportWorker(f, 'Bremen'))[0]['ok']          # fewer than 1000 before
    _fill(db, 'Saarland', 1000)
    f = _write(tmp_path / 'sl.txt', [_hkde_line('Saarbrücken', 'Neu', str(i), 350000.0 + i, 5450000.0)
                                     for i in range(1, 501)])
    assert _run(dbi.DbImportWorker(f, 'Saarland'))[0]['ok']        # exactly 50 % is fine


def test_replace_false_has_no_shrink_guard(db, tmp_path):
    _fill(db, 'Bremen', 1500)
    f = _write(tmp_path / 'hb.txt', [_hkde_line('Bremen', 'Neu', '1', 487000.0, 5885000.0)])
    assert _run(dbi.DbImportWorker(f, 'Bremen', replace=False))[0]['ok']
    assert _count(db, 'Bremen') == 1501


# ── A3: NULL attributes, numeric PLZ ─────────────────────────────────────────

class _FakeNullVariant:
    """Behaves like QVariant NULL under PyQt5: str() gives 'NULL'."""
    def isNull(self):
        return True

    def __str__(self):
        return 'NULL'


def test_attr_text_and_plz_text():
    assert pr.attr_text(None) == ''
    assert pr.attr_text(_FakeNullVariant()) == ''
    assert pr.attr_text(' Am Wall ') == 'Am Wall'
    assert pr.attr_text(12.0) == '12' and pr.attr_text(12) == '12' and pr.attr_text(float('nan')) == ''
    assert pr.plz_text(1067) == '01067' and pr.plz_text(1067.0) == '01067'
    assert pr.plz_text('1067') == '01067' and pr.plz_text('01067') == '01067'
    assert pr.plz_text(_FakeNullVariant()) == '' and pr.plz_text('28195') == '28195'
    assert pr.plz_text('D-1067') == 'D-1067'                     # not all digits: unchanged


def test_py_resolver_numeric_plz_property(tmp_path):
    p = _write_areas(str(tmp_path / 'n.geojson'), [(1067, _square(0, 0, 10, 10))], gz=False)
    r = pr.PyPlzResolver(p)
    assert r.ok and r.lookup(5, 5, 25832) == '01067'


# ── A4 / B26: municipality names ─────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('Stadtgemeinde Bremen', 'Bremen'),
    ('Stadtgemeinde Bremerhaven', 'Bremerhaven'),
    ('Stadt Chemnitz', 'Chemnitz'),
    ('Neubrandenburg, Vier-Tore-Stadt', 'Neubrandenburg'),
    ('Güstrow, Barlachstadt', 'Güstrow'),
    ('Rostock, Hanse- und Universitätsstadt', 'Rostock'),
    ('Wetterzeube, Stadt', 'Wetterzeube'),
    ('Harsefeld, Flecken', 'Harsefeld'),
    ('Magdeburg, Landeshauptstadt', 'Magdeburg'),
    ('Kalbe (Milde)', 'Kalbe (Milde)'),
    ('Stadt', 'Stadt'),
    ('Stadtallendorf', 'Stadtallendorf'),
])
def test_clean_gemeinde(raw, expected):
    assert dbi._clean_gemeinde(raw) == expected


def test_gml_import_cleans_gemeinde(db, tmp_path):
    f = tmp_path / 'hb.gml'
    f.write_text(_collection([
        _name('AdminUnitName', 'AdminUnitName_1', 'Freie Hansestadt Bremen'),
        _name('AdminUnitName', 'AdminUnitName_2', 'Stadtgemeinde Bremen'),
        _name('ThoroughfareName', 'ThoroughfareName_S', 'Am Wall'),
        _address('A1', '12', 487000.0, 5885000.0, ['AdminUnitName_1', 'AdminUnitName_2', 'ThoroughfareName_S'],
                 epsg=25832),
    ]), encoding='utf-8')
    res, _ = _run(gml.GmlImportWorker(str(f), 'Bremen'))
    assert res['ok'], res
    assert _rows(db, 'Bremen') == [('Bremen', 'Am Wall', '12', '', '')]


# ── A6: encoding detection ───────────────────────────────────────────────────

def _ascii_lines(n):
    return [_hkde_line('Bochum', 'Ascii-Weg', str(i % 9000 + 1), 371000.0 + i, 5701000.0) for i in range(n)]


def test_cp1252_umlaut_after_20kb_ascii(db, tmp_path):
    lines = _ascii_lines(300)
    assert len('\r\n'.join(lines)) > 20000
    lines.append(_hkde_line('Bochum', 'Mühlenweg', '1', 371500.0, 5701500.0))
    f = _write(tmp_path / 'cp.txt', lines, encoding='cp1252')
    assert dbi._detect_encoding(f) == 'cp1252'
    res, _ = _run(dbi.DbImportWorker(f, NRW))
    assert res['ok'], res
    conn = db.get_connection()
    names = {r[0] for r in conn.execute("SELECT strasse FROM addresses WHERE bundesland = ?", (NRW,))}
    conn.close()
    assert 'Mühlenweg' in names and not any('\ufffd' in n for n in names)


def test_cp1252_preview(tmp_path):
    f = _write(tmp_path / 'cp.txt', [_hkde_line('Lüdenscheid', 'Straße', '1', 1.0, 2.0)], encoding='cp1252')
    assert dbi.DbImportWorker.preview_file(f, 1)[0]['gemeinde'] == 'Lüdenscheid'


def test_latin1_with_bytes_undefined_in_cp1252(tmp_path):
    p = tmp_path / 'l1.txt'
    p.write_bytes(_hkde_line('M\xfcnster', 'Weg\x9d', '1', 1.0, 2.0).encode('latin-1') + b'\r\n')
    assert dbi._detect_encoding(str(p)) == 'latin-1'


def test_utf8_bom_and_utf16(db, tmp_path):
    line = _hkde_line('Köln', 'Gürzenichstraße', '1', 356000.0, 5645000.0)
    f8 = _write(tmp_path / 'bom.txt', [line], encoding='utf-8-sig')
    assert dbi._detect_encoding(f8) == 'utf-8-sig'
    f16 = _write(tmp_path / 'u16.txt', [line], encoding='utf-16')
    assert dbi._detect_encoding(f16) == 'utf-16'
    res, _ = _run(dbi.DbImportWorker(f16, NRW))
    assert res['ok'], res
    assert _rows(db, NRW) == [('Köln', 'Gürzenichstraße', '1', '', '')]
    plain = _write(tmp_path / 'u8.txt', [line])
    assert dbi._detect_encoding(plain) == 'utf-8'


# ── B1: house numbers from one shapefile field ───────────────────────────────

def test_hausnummer_rows():
    h = dbi._hausnummer_rows
    assert h('12') == [('12', '')]
    assert h('12', 'b') == [('12', 'b')]
    assert h('') == [('', '')]
    assert h('12a') == [('12', 'a')]
    assert h('12 a') == [('12', 'a')]
    assert h('12-14') == [('12', '')]
    assert h('12;14') == [('12', ''), ('14', '')]
    assert h('12a', 'c') == [('12', 'c')]              # filled suffix field takes precedence
    assert h('84068997') == [] and h('12345') == [] and h('84068997a') == []
    assert h('ohne') == []


# ── B22 / B27: HK-DE format detection, Saarland zone ─────────────────────────

def _line26(first, land, gmd, plz, n=26):
    p = [''] * n
    p[0] = first
    p[4] = land
    p[8] = 'Bezirk'
    p[10] = gmd
    p[14] = 'Weg'
    p[15] = '1'
    p[17] = '33'
    p[18] = '390000.0'
    p[19] = '5820000.0'
    p[20] = plz
    return ';'.join(p)


def test_detect_format_berlin_without_plz_in_first_line(tmp_path):
    f = _write(tmp_path / 'be.txt', [_line26('', 'Berlin', 'Pankow', ''),
                                     _line26('', 'Berlin', 'Pankow', '13187')])
    assert dbi._detect_format(f) == 'berlin'
    f = _write(tmp_path / 'be2.txt', [_line26('', 'Berlin', 'Pankow', '')] * 3)
    assert dbi._detect_format(f) == 'berlin'                        # land name decides
    f = _write(tmp_path / 'st.txt', [_line26('', 'Sachsen-Anhalt', 'Halle', '')] * 3)
    assert dbi._detect_format(f) == 'sachsen_anhalt'


def test_detect_format_brandenburg_plz_in_later_line(tmp_path):
    lines = [_line26('X', 'Brandenburg', 'Potsdam', '')] * 50 + [_line26('X', 'Brandenburg', 'Potsdam', '14467')]
    assert dbi._detect_format(_write(tmp_path / 'bb.txt', lines)) == 'brandenburg'
    lines = [_line26('X', 'Baden-Württemberg', 'Ulm', '')] * 5
    assert dbi._detect_format(_write(tmp_path / 'bw.txt', lines)) == 'baden_wuerttemberg'


def test_saarland_zone_validated():
    assert dbi._parse_saarland_xy('32360468,695', '5473701,597') == ('32', 360468.695, 5473701.597)
    assert dbi._parse_saarland_xy('33360468,695', '5473701,597')[0] == '33'
    with pytest.raises(ValueError):
        dbi._parse_saarland_xy('31360468,695', '5473701,597')
    with pytest.raises(ValueError):
        dbi._parse_saarland_xy('360468,695', '5473701,597')


# ── GML: B6 srsName, B24 PLZ, B9 broken file, B10 preview, B25 DB errors ─────

@pytest.mark.parametrize('srs, expected', [
    ('EPSG:25833', (25833, False)),
    ('urn:ogc:def:crs:EPSG::25833', (25833, False)),
    ('urn:ogc:def:crs:EPSG:6.12:25833', (25833, False)),
    ('http://www.opengis.net/def/crs/EPSG/0/25833', (25833, False)),
    ('https://www.opengis.net/def/crs/EPSG/0/25832', (25832, False)),
    ('http://www.opengis.net/gml/srs/epsg.xml#25833', (25833, False)),
    ('urn:adv:crs:ETRS89_UTM33', (25833, False)),
    ('urn:adv:crs:ETRS89_UTM32', (25832, False)),
    ('urn:ogc:def:crs:EPSG::4258', (4258, True)),
    ('http://www.opengis.net/def/crs/EPSG/0/4326', (4326, True)),
    ('EPSG:4326', (4326, False)),
    ('', (25832, False)),
])
def test_parse_srs_name(srs, expected):
    assert gml.parse_srs_name(srs) == expected


def test_gml_latlon_axis_swap(db, tmp_path):
    f = tmp_path / 'g.gml'
    f.write_text(_collection([
        _name('AdminUnitName', 'AU1', 'Testland'),
        _name('ThoroughfareName', 'TF1', 'Breite Straße'),
        _address('A1', '1', 52.5, 13.4, ['AU1', 'TF1'], epsg=4258),
    ]), encoding='utf-8')
    res, _ = _run(gml.GmlImportWorker(str(f), 'Berlin'))
    assert res['ok'], res
    conn = db.get_connection()
    row = conn.execute("SELECT x, y, epsg, gemeinde FROM addresses WHERE strasse = 'Breite Straße' "
                       "AND bundesland = 'Berlin'").fetchone()
    conn.close()
    assert tuple(row) == (13.4, 52.5, 4258, 'Testland')              # x = longitude; AU without prefix


def _postal(gid, code):
    return (f'<wfs:member><ad:PostalDescriptor gml:id="{gid}"><ad:postCode>{code}</ad:postCode>'
            f'</ad:PostalDescriptor></wfs:member>')


def test_gml_plz_from_postcode_and_5_digit_check(db, tmp_path):
    f = tmp_path / 'p.gml'
    f.write_text(_collection([
        _name('AdminUnitName', 'AdminUnitName_1', 'Land'),
        _name('AdminUnitName', 'AdminUnitName_2', 'Hamburg'),
        _name('ThoroughfareName', 'TF', 'Weg'),
        _postal('PD.4711', '22359'),                      # id without the PLZ
        _postal('PostalDescriptor_99', '123'),            # invalid postCode and suffix
        _address('A1', '1', 565000.0, 5934000.0, ['AdminUnitName_1', 'AdminUnitName_2', 'TF', 'PD.4711'],
                 epsg=25832),
        _address('A2', '2', 565010.0, 5934000.0, ['AdminUnitName_1', 'AdminUnitName_2', 'TF',
                                                  'PostalDescriptor_99'], epsg=25832),
        _address('A3', '3', 565020.0, 5934000.0, ['AdminUnitName_1', 'AdminUnitName_2', 'TF',
                                                  'PostalDescriptor_2010_06'], epsg=25832),
        _address('A4', '4', 565030.0, 5934000.0, ['AdminUnitName_1', 'AdminUnitName_2', 'TF',
                                                  'PostalDescriptor_20354'], epsg=25832),
    ]), encoding='utf-8')
    res, _ = _run(gml.GmlImportWorker(str(f), 'Hamburg'))
    assert res['ok'], res
    assert [r[4] for r in _rows(db, 'Hamburg')] == ['22359', '', '', '20354']


def test_gml_broken_file_aborts_cleanly(db, tmp_path):
    before = _rows(db, 'Hamburg')
    good = tmp_path / 'names.gml'
    good.write_text(_collection([_name('ThoroughfareName', 'TF', 'Weg'),
                                 _address('A1', '1', 565000.0, 5934000.0, ['TF'], epsg=25832)]),
                    encoding='utf-8')
    broken = tmp_path / 'kaputt.gml'
    broken.write_text(_collection([_address('A2', '2', 565010.0, 5934000.0, ['TF'])])[:-40],
                      encoding='utf-8')
    res, logs = _run(gml.GmlImportWorker([str(good), str(broken)], 'Hamburg'))
    assert res['ok'] is False
    assert 'kaputt.gml' in res['msg'] and 'beschädigt' in res['msg'] and 'bleibt erhalten' in res['msg']
    assert not any('lesbaren' in m for m in logs)
    assert _rows(db, 'Hamburg') == before


def test_gml_preview_standard_address_number_href(tmp_path):
    f = tmp_path / 'p.gml'
    f.write_text(_collection([_address('A1', '7', 1.0, 2.0, ['ThoroughfareName_S', 'PostalDescriptor_22767'])]),
                 encoding='utf-8')
    prev = gml.GmlImportWorker.preview_file(str(f), n=1)
    assert prev == [{'hausnummer': '7', 'plz': '22767', 'strasse_ref': 'ThoroughfareName_S',
                     'x': '1.0', 'y': '2.0'}]
    assert gml._designator_kind('http://x/LocatorDesignatorTypeValue/addressNumberExtension') == \
        'addressNumberExtension'
    assert gml._designator_kind('http://x/LocatorDesignatorTypeValue/addressNumber/') == 'addressNumber'


def test_gml_db_error_aborts_instead_of_skipping(db, tmp_path, monkeypatch):
    before = _rows(db, 'Hamburg')
    f = tmp_path / 'g.gml'
    f.write_text(_collection([_name('ThoroughfareName', 'TF', 'Weg'),
                              _address('A1', '1', 565000.0, 5934000.0, ['TF'], epsg=25832)]),
                 encoding='utf-8')
    monkeypatch.setattr(dbi.AddressSink, 'INSERT_SQL', 'INSERT INTO gibt_es_nicht VALUES (?)')
    res, _ = _run(gml.GmlImportWorker(str(f), 'Hamburg'))
    assert res['ok'] is False and 'Import fehlgeschlagen' in res['msg']
    assert _rows(db, 'Hamburg') == before


def test_workers_have_no_plz_layer_route():
    for cls in (dbi.DbImportWorker, gml.GmlImportWorker):
        assert not hasattr(cls, 'set_plz_layer')


# ── PLZ post-processing: A29 cancel, B41 streaming, A30 cache key ────────────

def _nrw_without_plz(db, n):
    conn = sqlite3.connect(db.DB_PATH)
    lg.ensure_plz_herkunft(conn)
    conn.execute("DELETE FROM addresses WHERE bundesland = ?", (NRW,))
    conn.executemany(
        "INSERT INTO addresses (bundesland, gemeinde, plz, strasse, hausnummer, epsg, x, y, "
        "strasse_norm, gemeinde_norm) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(NRW, 'Bochum', '', 'W', str(i), 25832, 371000.0 + i, 5701000.0, 'w', 'bochum') for i in range(n)])
    conn.commit()
    conn.close()


@pytest.mark.usefixtures('areas')
def test_nachlauf_streams_in_chunks(db, monkeypatch):
    _nrw_without_plz(db, 7)
    monkeypatch.setattr(pr, '_NACHLAUF_CHUNK', 2)
    prog = []
    res = pr.ergaenze_plz(db.DB_PATH, bundeslaender=[NRW], progress=lambda d, t, m: prog.append((d, t)))
    assert res == {NRW: 7}
    assert prog == [(2, 7), (4, 7), (6, 7), (7, 7)]
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland = ? AND plz = '44869' "
                        "AND plz_herkunft = 'gebiet'", (NRW,)).fetchone()[0] == 7
    conn.close()


@pytest.mark.usefixtures('areas')
def test_nachlauf_cancel_does_not_record_new_key(db, monkeypatch):
    _nrw_without_plz(db, 6)
    monkeypatch.setattr(pr, '_NACHLAUF_CHUNK', 2)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 2            # state start + first chunk pass, then cancel

    res = pr.ergaenze_plz(db.DB_PATH, bundeslaender=[NRW], cancel=cancel)
    assert res == {}                                                  # not reported as done
    conn = sqlite3.connect(db.DB_PATH)
    lg.ensure_plz_meta(conn)
    key = conn.execute("SELECT quelle_key FROM plz_meta WHERE bundesland = ?", (NRW,)).fetchone()
    filled = conn.execute("SELECT COUNT(*) FROM addresses WHERE bundesland = ? AND plz <> ''",
                          (NRW,)).fetchone()[0]
    conn.close()
    assert key is None                                                # new version NOT recorded
    assert filled == 2                                                # correct partial work kept
    # the next run completes the state and records the version
    assert pr.ergaenze_plz(db.DB_PATH, bundeslaender=[NRW]) == {NRW: 4}


def test_override_key_includes_path_layer_and_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, '_ENABLED', True)
    shp = tmp_path / 'plz.shp'
    dbf = tmp_path / 'plz.dbf'
    shp.write_bytes(b'x')
    dbf.write_bytes(b'y')
    os.utime(shp, (1_700_000_000, 1_700_000_000))
    os.utime(dbf, (1_700_000_000, 1_700_000_000))
    try:
        pr.set_override({'path': str(shp), 'field': 'plz'})
        k1 = pr.active_source()['key']
        assert os.path.abspath(str(shp)) in k1
        os.utime(dbf, (1_700_100_000, 1_700_100_000))                # only the .dbf changes
        k2 = pr.active_source()['key']
        assert k2 != k1
        gpkg = tmp_path / 'plz.gpkg'
        gpkg.write_bytes(b'z')
        pr.set_override({'path': str(gpkg), 'field': 'plz', 'layer': 'a'})
        ka = pr.active_source()['key']
        pr.set_override({'path': str(gpkg), 'field': 'plz', 'layer': 'b'})
        assert pr.active_source()['key'] != ka                        # other GeoPackage layer
        other = tmp_path / 'sub'
        other.mkdir()
        g2 = other / 'plz.gpkg'
        g2.write_bytes(b'z')
        os.utime(g2, (os.path.getmtime(gpkg),) * 2)
        pr.set_override({'path': str(g2), 'field': 'plz', 'layer': 'b'})
        assert pr.active_source()['key'] != ka                        # same name, other folder
    finally:
        pr.set_override(None)


@pytest.mark.usefixtures('areas')
def test_build_resolver_logged_uses_active_source_only():
    s = pr.build_resolver_logged('C:/gibt/es/nicht.gpkg', 'plz', 'x')
    assert s is not None and s.source['key'] == pr.active_source()['key']


# ── hk_by_row (shared HK-BY row) and A5 duplicate detection ─────────────────

def test_hk_by_row():
    row = oc.hk_by_row('09', 'Bayern', 'Oberdorf', 'Testdorf', 'Dorf;straße\n', '1', 'a', '32',
                       689000.123, 5335000.456, '85748', 'Testdorf', ident='OSMN1234567890123456')
    assert len(row) == len(oc.HK_BY_HEADER) == 24
    h = dict(zip(oc.HK_BY_HEADER, row))
    assert (h['nba'], h['oid'], h['landschl'], h['land'], h['kreis'], h['gmd']) == \
        ('N', 'OSMN123456789012', '09', 'Bayern', 'Oberdorf', 'Testdorf')
    assert (h['str'], h['hnr'], h['adz'], h['zone'], h['ostwert'], h['nordwert']) == \
        ('Dorf,straße', '1', 'a', '32', '689000.12', '5335000.46')
    assert (h['postplz'], h['postonm'], h['ott'], h['postott'], h['qua']) == ('85748', 'Testdorf', '', '', '')
    row = oc.hk_by_row('03', 'Niedersachsen', 'K', 'G', 'S', '2', '', '33', 1, 2, '', 'G', ident=7,
                       qua='A', ott='O')
    h = dict(zip(oc.HK_BY_HEADER, row))
    assert (h['oid'], h['qua'], h['ott'], h['postott'], h['zone']) == ('7', 'A', 'O', 'O', '33')


def test_dup_index_near_and_far():
    d = oc._DupIndex()
    key = ('hauptstraße', '1', '', '', 'testdorf')
    assert d.check_add(1000.0, 1000.0, *key) is False
    assert d.check_add(1030.0, 1010.0, *key) is True          # building next to the address point
    assert d.check_add(3000.0, 1000.0, *key) is False         # same address 2 km away: other village
    assert d.check_add(1000.0, 1000.0, 'hauptstraße', '1', '', '', 'nachbarort') is False
    key2 = ('kirchweg', '5', '', '', 'testdorf')
    assert d.check_add(1495.0, 1000.0, *key2) is False
    assert d.check_add(1520.0, 1000.0, *key2) is True         # 25 m away across the cell border
