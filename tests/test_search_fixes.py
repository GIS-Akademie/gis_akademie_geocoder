# -*- coding: utf-8 -*-
"""Regression tests for the search fixes of the code review (A19–A25, A32,
B4, B21, B29, B12) and the rebuild/lock interface. Each test builds its own
small database (helper make_db) so the scenarios stay independent of the
shared fixture data in conftest.py."""
import sqlite3

import pytest

from conftest import lg


def make_db(tmp_path, addresses, lookup=True, herkunft=None):
    """addresses: (bundesland, kreis, gemeinde, plz, strasse, hn, zusatz, epsg, x, y).
    herkunft: optional {index: 'gebiet'} for plz_herkunft."""
    path = str(tmp_path / 'fix.db')
    conn = sqlite3.connect(path)
    lg.init_db(conn)
    rows = []
    for i, (bl, kreis, gem, plz, strasse, hn, zusatz, epsg, x, y) in enumerate(addresses):
        rows.append((bl, kreis, gem, plz, strasse, hn, zusatz, epsg, float(x), float(y),
                     lg.normalize(strasse), lg._core_gemeinde(gem),
                     (herkunft or {}).get(i)))
    conn.executemany(
        "INSERT INTO addresses (bundesland, kreis, gemeinde, plz, strasse, hausnummer,"
        " hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm, plz_herkunft)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    lg.set_db_path(path)
    if lookup:
        lg.rebuild_lookup_tables(conn)
        conn.commit()
    conn.close()
    return path


def top(**kw):
    hits = lg.search(max_results=3, **kw)
    assert hits, f'kein Treffer für {kw}'
    return hits[0]


# ── A19: PLZ with lost leading zero ──────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('1067', '01067'), (' 1067 ', '01067'), ('1067.0', '01067'), (1067, '01067'),
    (1067.0, '01067'), ('01067', '01067'), ('44869', '44869'), ('44869.0', '44869'),
    ('', ''), (None, ''), ('  ', ''), ('D-01067', 'D-01067'), ('123', '123'),
])
def test_normalize_plz(raw, expected):
    assert lg.normalize_plz(raw) == expected


def test_numeric_plz_is_padded_everywhere(tmp_path):
    make_db(tmp_path, [
        ('Sachsen', '', 'Dresden', '01067', 'Prager Straße', '1', '', 25833, 411000, 5656000),
        ('Berlin', '', 'Berlin', '10117', 'Prager Straße', '1', '', 25833, 391000, 5819000),
    ])
    assert lg.bundesland_from_plz('1067') == 'Sachsen'
    assert lg.bundesland_from_plz('1067.0') == 'Sachsen'
    h = top(strasse='Prager Straße 1', plz='1067')
    assert (h['gemeinde'], h['plz'], h['hn_score']) == ('Dresden', '01067', 1.0)
    assert lg.locate_locality(plz='1067.0')['bundesland'] == 'Sachsen'


# ── A21: second attempt ranks the anchor candidates ─────────────────────────

def _berlin_karl(tmp_path, extra=()):
    a = []
    # Karl-Liebknecht first: an unordered LIMIT used to return it
    for n in range(1, 300):
        a.append(('Berlin', 'Mitte', 'Berlin', '10178', 'Karl-Liebknecht-Straße', str(n), '',
                  25833, 392000 + n, 5821000))
    for n in range(1, 300):
        a.append(('Berlin', 'Neukölln', 'Berlin', '12043', 'Karl-Marx-Straße', str(n), '',
                  25833, 393000 + n, 5815000))
    a.extend(extra)
    make_db(tmp_path, a)


def test_spaces_instead_of_hyphens_find_right_street(tmp_path):
    _berlin_karl(tmp_path)
    for kw in (dict(strasse='Karl Marx Str. 250', gemeinde='Berlin'),
               dict(strasse='Karl Marx Str.', hausnummer='250', plz='12043'),
               dict(strasse='karl-marx-straße 250', gemeinde='Berlin')):
        h = top(**kw)
        assert (h['strasse'], h['hausnummer']) == ('Karl-Marx-Straße', '250'), kw


def test_safe_prefix_shared_by_separator_variants():
    p = lg._safe_prefix
    assert p('karl marx straße') == p('karl-marx-straße') == 'karl'
    assert p('dr otto suhr platz') == p('dr.-otto-suhr-platz') == 'dr'
    assert p('zeil') == 'zeil'
    assert p('a-straße') == 'a-str'      # separator before 2 chars: no cut


def test_anchor_attempt_ranks_by_similarity(tmp_path):
    # 'Otto Suhr Platz' does not share the prefix with 'Dr.-Otto-Suhr-Platz';
    # the anchor search ('%otto%' …) must pick it over 'Otto-Braun-Straße'.
    extra = [('Berlin', 'Mitte', 'Berlin', '10178', 'Otto-Braun-Straße', str(n), '',
              25833, 392500 + n, 5821500) for n in range(1, 500)]
    extra.append(('Berlin', 'Charlottenburg', 'Berlin', '10585', 'Dr.-Otto-Suhr-Platz',
                  '1', '', 25833, 386000, 5819000))
    _berlin_karl(tmp_path, extra)
    h = top(strasse='Otto Suhr Platz 1', gemeinde='Berlin')
    assert h['strasse'] == 'Dr.-Otto-Suhr-Platz'


# ── A23: ss/ß straddling the prefix cut ─────────────────────────────────────

def test_ss_straddling_prefix(tmp_path):
    make_db(tmp_path, [
        ('Berlin', '', 'Berlin', '14052', 'Preußenallee', '3', '', 25833, 383000, 5818000),
        ('Berlin', '', 'Berlin', '10557', 'Straße des 17. Juni', '135', '', 25833, 388000, 5820000),
    ])
    assert lg._safe_prefix('preussenallee') == 'preu'
    assert lg._safe_prefix('strasse des 17 juni') == 'stra'
    h = top(strasse='Preussenallee 3', gemeinde='Berlin')
    assert (h['strasse'], h['hausnummer']) == ('Preußenallee', '3')
    h = top(strasse='STRASSE DES 17. JUNI 135', plz='10557')
    assert h['strasse'] == 'Straße des 17. Juni'


def test_anchor_words_match_ss_and_sz():
    assert lg._anchor_words('preussenallee') == ['enallee']
    assert 'marx' in lg._anchor_words('karl marx straße')
    assert lg._anchor_words('am markt') == ['markt']


# ── A24: anchor fallback without a PLZ-guessed Bundesland ────────────────────

def test_ambiguous_plz_prefixes_unmapped():
    assert lg.bundesland_from_plz('14467') == ''      # Potsdam (BB) / Berlin
    assert lg.bundesland_from_plz('49074') == ''      # Osnabrück (NI) / NRW
    assert lg.bundesland_from_plz('10117') == 'Berlin'
    assert lg.bundesland_from_plz('48143') == 'Nordrhein-Westfalen'


def test_anchor_fallback_ignores_guessed_bundesland(tmp_path):
    make_db(tmp_path, [
        ('Berlin', '', 'Berlin', '10557', 'Unter den Linden', '1', '', 25833, 389000, 5820000),
        ('Brandenburg', '', 'Potsdam', '14467', 'Dr.-Hans-Meier-Straße', '5', '', 25833, 368000, 5806000),
        ('Brandenburg', '', 'Potsdam', '14467', 'Dr.-Hans-Meier-Straße', '7', '', 25833, 368020, 5806000),
    ])
    for bl in ('Berlin', None, 'Brandenburg'):
        h = top(strasse='Hans-Meier-Str.', hausnummer='5', plz='14467', gemeinde='Potsdam',
                bundesland=bl)
        assert (h['strasse'], h['bundesland']) == ('Dr.-Hans-Meier-Straße', 'Brandenburg'), bl


# ── A22: same street name in two Ortsteile ──────────────────────────────────

def _two_ortsteile(tmp_path):
    a = [('Niedersachsen', 'Landkreis X', 'Musterstadt', '31111', 'Dorfstraße', str(n), '',
          25832, 500000 + n * 10, 5800000) for n in range(1, 11)]
    a += [('Niedersachsen', 'Landkreis X', 'Musterstadt', '31122', 'Dorfstraße', str(n), '',
           25832, 510000 + n * 10, 5800000) for n in range(1, 31)]
    make_db(tmp_path, a)


def test_street_representative_uses_plz(tmp_path):
    _two_ortsteile(tmp_path)
    rp = lg.street_representative('dorfstraße', 'musterstadt', plz='31111', hausnummer='12')
    assert (rp['kind'], rp['hausnummer'], rp['count']) == ('nearest', '10', 10)
    rp = lg.street_representative('dorfstraße', 'musterstadt', plz='31111')
    assert rp['kind'] == 'center' and rp['x'] < 505000
    # PLZ without rows of the street: fall back to the Gemeinde
    rp = lg.street_representative('dorfstraße', 'musterstadt', plz='99999', hausnummer='12')
    assert rp['hausnummer'] == '12' and rp['count'] == 40


def test_exact_hn_in_other_ortsteil_does_not_beat_given_plz(tmp_path):
    _two_ortsteile(tmp_path)
    h = top(strasse='Dorfstraße', hausnummer='12', plz='31111', gemeinde='Musterstadt')
    assert h['plz'] == '31111'
    h = top(strasse='Dorfstraße', hausnummer='3', plz='31111', gemeinde='Musterstadt')
    assert (h['plz'], h['hausnummer'], h['hn_score']) == ('31111', '3', 1.0)


def test_exact_hn_across_nearby_plz_border_still_wins(tmp_path):
    # One street continuing across a PLZ border (100 m): a slightly wrong PLZ
    # must not hide the exact house number.
    a = [('Niedersachsen', '', 'Musterstadt', '31111', 'Lange Straße', str(n), '',
          25832, 500000 + n * 10, 5800000) for n in range(1, 11)]
    a += [('Niedersachsen', '', 'Musterstadt', '31122', 'Lange Straße', str(n), '',
           25832, 500000 + n * 10, 5800000) for n in range(11, 21)]
    make_db(tmp_path, a)
    h = top(strasse='Lange Straße', hausnummer='15', plz='31111', gemeinde='Musterstadt')
    assert (h['hausnummer'], h['hn_score'], h['plz']) == ('15', 1.0, '31122')


# ── A25: sub-numbers ('12/1') ────────────────────────────────────────────────

def test_sub_number_not_exact_on_main_number(tmp_path):
    make_db(tmp_path, [
        ('Baden-Württemberg', '', 'Stuttgart', '70173', 'Königstraße', '12', '', 25832, 513000, 5404000),
        ('Baden-Württemberg', '', 'Stuttgart', '70173', 'Königstraße', '12', '/1', 25832, 513500, 5404500),
        # HK-DE BW stores the sub-number without the slash
        ('Baden-Württemberg', '', 'Stuttgart', '70173', 'Königstraße', '16', '1', 25832, 513600, 5404600),
        ('Baden-Württemberg', '', 'Stuttgart', '70173', 'Königstraße', '20', '', 25832, 513700, 5404700),
    ])
    hits = lg.search('Königstraße', '12/1', '70173', 'Stuttgart', max_results=3)
    assert (hits[0]['hausnummer'], hits[0]['hn_score'], hits[0]['x']) == ('12/1', 1.0, 513500.0)
    assert hits[1]['hausnummer'] == '12' and hits[1]['hn_score'] == 0.5
    h = top(strasse='Königstraße 16/1', gemeinde='Stuttgart')
    assert (h['hausnummer'], h['hn_score']) == ('16/1', 1.0)
    h = top(strasse='Königstraße', hausnummer='20/1', gemeinde='Stuttgart')
    assert (h['hausnummer'], h['hn_score']) == ('20', 0.5)       # at most partial
    # '161' is a different number, not 16/1
    assert all(x['hn_score'] == 0.0 for x in
               lg.search('Königstraße', '161', '', 'Stuttgart', max_results=5))


def test_row_hausnummer_display():
    assert lg._row_hausnummer('16', '1') == '16/1'
    assert lg._row_hausnummer('12', '/1') == '12/1'
    assert lg._row_hausnummer('3', 'A') == '3A'
    assert lg._row_hausnummer('3', None) == '3'


# ── B4: house number without a space ────────────────────────────────────────

@pytest.mark.parametrize('raw, street, hn', [
    ('Hauptstr.12', 'hauptstraße', '12'),
    ('Hauptstraße12', 'hauptstraße', '12'),
    ('Hauptstr.12a', 'hauptstraße', '12a'),
    ('An der B5', 'an der b5', ''),             # numbered road name stays intact
    ('Q7', 'q7', ''),
])
def test_glued_hausnummer(raw, street, hn):
    assert lg._extract_hausnummer(lg.normalize(raw)) == (street, hn)


def test_glued_hausnummer_search(tmp_path):
    make_db(tmp_path, [
        ('Hessen', '', 'Kassel', '34117', 'Hauptstraße', '12', '', 25832, 534120, 5685000),
        ('Hessen', '', 'Kassel', '34117', 'Hauptstraße', '14', '', 25832, 534140, 5685000),
    ])
    h = top(strasse='Hauptstr.12', gemeinde='Kassel')
    assert (h['hausnummer'], h['hn_score']) == ('12', 1.0)


# ── B29: PLZ derived from PLZ areas ─────────────────────────────────────────

def test_area_plz_mismatch_is_neutral(tmp_path):
    make_db(tmp_path, [
        ('Niedersachsen', '', 'Bergen', '29303', 'Hauptstraße', '1', '', 25832, 580000, 5845000),
        ('Niedersachsen', '', 'Celle', '29221', 'Hauptstraße', '1', '', 25832, 590000, 5835000),
        ('Niedersachsen', '', 'Hermannsburg', '', 'Hauptstraße', '1', '', 25832, 595000, 5850000),
    ], herkunft={0: 'gebiet'})
    gebiet = top(strasse='Hauptstraße 1', plz='29999', gemeinde='Bergen')
    ohne   = top(strasse='Hauptstraße 1', plz='29999', gemeinde='Hermannsburg')
    amtl   = top(strasse='Hauptstraße 1', plz='29999', gemeinde='Celle')
    assert gebiet['score'] == ohne['score'] == 1.0          # like a missing PLZ
    assert amtl['score'] < gebiet['score']                  # official mismatch stays one


# ── B21: directory of an older schema on a read-only DB ──────────────────────

def test_old_directory_used_when_rebuild_impossible(tmp_path, monkeypatch):
    make_db(tmp_path, [
        ('Hessen', '', 'Frankfurt am Main', '60313', 'Zeil', '2', '', 25832, 477000, 5551000),
        ('Brandenburg', '', 'Frankfurt (Oder)', '15230', 'Bahnhofstraße', '1', '', 25833, 470000, 5800000),
    ])
    conn = sqlite3.connect(lg.DB_PATH)
    conn.execute("UPDATE lookup_meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    lg._lookup_available.clear()

    def fail(*a, **k):
        raise sqlite3.OperationalError('attempt to write a readonly database')
    monkeypatch.setattr(lg, 'rebuild_lookup_tables', fail)
    assert lg.ensure_lookup_tables() is False
    assert lg._lookup_available[lg.DB_PATH] is True
    h = top(strasse='Zeil 2', gemeinde='Frankfurt')         # needs the directory
    assert h['gemeinde'] == 'Frankfurt am Main'


# ── Rebuild: one transaction, cancel, ANALYZE ───────────────────────────────

def _directory_rows(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT gemeinde_norm, bundesland, n FROM gemeinde_index"))


def test_rebuild_runs_analyze(tmp_path):
    make_db(tmp_path, [('Hessen', '', 'Kassel', '34117', 'Zeil', '1', '', 25832, 1, 2)])
    conn = sqlite3.connect(lg.DB_PATH)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert 'sqlite_stat1' in names
        assert conn.execute("SELECT COUNT(*) FROM sqlite_stat1 WHERE idx = 'idx_gem_str'"
                            ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize('cancel_after', [1, 2, 3, 5])
def test_rebuild_cancel_keeps_old_tables(tmp_path, cancel_after):
    make_db(tmp_path, [
        ('Hessen', '', 'Kassel', '34117', 'Zeil', '1', '', 25832, 1, 2),
        ('Bayern', '', 'München', '81377', 'Lorettoplatz', '7', '', 25832, 3, 4),
    ])
    conn = sqlite3.connect(lg.DB_PATH)
    before = _directory_rows(conn)
    conn.execute("INSERT INTO addresses (bundesland, strasse, x, y, gemeinde_norm) "
                 "VALUES ('Berlin', 'Neu', 0, 0, 'berlin')")
    conn.commit()
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) >= cancel_after
    with pytest.raises(lg.RebuildCancelled):
        lg.rebuild_lookup_tables(conn, cancel=cancel)
    conn.commit()                                            # harmless for the caller
    assert not conn.in_transaction
    assert _directory_rows(conn) == before                   # old directory intact
    assert conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0] == 3
    conn.close()


def test_rebuild_cancel_inside_caller_transaction(tmp_path):
    """Import worker case: DELETE/INSERT pending, rebuild in a SAVEPOINT."""
    make_db(tmp_path, [('Hessen', '', 'Kassel', '34117', 'Zeil', '1', '', 25832, 1, 2)])
    conn = sqlite3.connect(lg.DB_PATH)
    before = _directory_rows(conn)
    conn.execute("INSERT INTO addresses (bundesland, strasse, x, y, gemeinde_norm) "
                 "VALUES ('Berlin', 'Neu', 0, 0, 'berlin')")
    assert conn.in_transaction
    with pytest.raises(lg.RebuildCancelled):
        lg.rebuild_lookup_tables(conn, cancel=lambda: _directory_rows(conn) == [])
    assert conn.in_transaction                               # caller's transaction alive
    assert _directory_rows(conn) == before
    lg.rebuild_lookup_tables(conn)                           # now without cancel
    conn.commit()
    assert ('berlin', 'Berlin', 1) in _directory_rows(conn)
    conn.close()


def test_rebuild_is_one_transaction(tmp_path):
    make_db(tmp_path, [('Hessen', '', 'Kassel', '34117', 'Zeil', '1', '', 25832, 1, 2)])
    conn = sqlite3.connect(lg.DB_PATH)
    lg.rebuild_lookup_tables(conn)
    assert conn.in_transaction                               # DDL not autocommitted
    other = sqlite3.connect(lg.DB_PATH, timeout=0.1)
    try:
        # the other connection still sees the committed (old) state
        assert other.execute("SELECT COUNT(*) FROM gemeinde_index").fetchone()[0] == 1
    finally:
        other.close()
    conn.commit()
    conn.close()


# ── count_addresses / db_state ──────────────────────────────────────────────

def test_count_addresses(db):
    assert db.count_addresses('Hessen') == 14
    assert db.count_addresses('Saarland') == 0
    conn = db.get_connection()
    try:
        assert db.count_addresses('Berlin', conn) == 4
    finally:
        conn.close()


def test_count_addresses_without_db(tmp_path):
    lg.set_db_path(str(tmp_path / 'nichts.db'))
    assert lg.count_addresses('Hessen') == 0
    assert lg.db_state() == 'missing' and lg.db_exists() is False


def test_db_state_locked_is_not_missing(db):
    writer = sqlite3.connect(db.DB_PATH)
    writer.execute('BEGIN EXCLUSIVE')
    try:
        assert db.db_state(timeout=0.05) == 'locked'
        conn = db.get_connection(timeout=0.05)
        try:
            with pytest.raises(db.DatabaseLocked):
                db.search('Elchweg 3', gemeinde='Bochum', conn=conn)
        finally:
            conn.close()
    finally:
        writer.rollback()
        writer.close()
    assert db.db_state() == 'ok'


# ── B12 / B43: diagnosis ─────────────────────────────────────────────────────

def test_diagnose_reports_error_cause(db):
    conn = db.get_connection()
    details = []
    try:
        conn.execute('DROP TABLE gemeinde_index')
        conn.execute('DROP TABLE ortsteil_index')
        conn.execute('DROP TABLE addresses')
        conn.commit()
        db._lookup_available.clear()
        txt = db.diagnose_search('Elchweg 1', gemeinde='Bochum', conn=conn,
                                 detail=details.append)
    finally:
        conn.close()
    assert txt == 'Ursache nicht ermittelbar: Datenbankfehler'
    assert details and 'no such table' in details[0]


def test_diagnose_with_shared_connection(db):
    conn = db.get_connection()
    try:
        assert db.diagnose_search('Gibtsnichtweg 1', gemeinde='Blankenese', conn=conn) == \
            'Straße „Gibtsnichtweg 1“ in Blankenese nicht gefunden'
        assert db.diagnose_search('Gibtsnichtweg 1', plz='44869', conn=conn) == \
            'Straße „Gibtsnichtweg 1“ in PLZ 44869 nicht gefunden'
        assert db.diagnose_search('Elchweg 1', plz='44869', conn=conn) == \
            'Keine ausreichend ähnliche Adresse gefunden'
    finally:
        conn.close()


def test_resolver_info_without_legacy_args():
    class S:
        def info(self):
            return {'layer': 'x', 'quelle_key': 'k'}
    assert lg.resolver_info(S()) == {'layer': 'x', 'quelle_key': 'k'}
    assert lg.resolver_info(None) is None


# ── B39: renew the directory of single states only ──────────────────────────

def _directory(conn):
    def rows(sql):
        return sorted(tuple(r) for r in conn.execute(sql).fetchall())
    return (rows("SELECT * FROM gemeinde_index"), rows("SELECT * FROM ortsteil_index"),
            rows("SELECT * FROM bundesland_index"),
            dict(tuple(r) for r in conn.execute("SELECT key, value FROM lookup_meta").fetchall()))


def test_partial_rebuild_equals_full_rebuild(db):
    conn = db.get_connection()
    lg.rebuild_lookup_tables(conn)
    conn.commit()
    bl, = conn.execute("SELECT bundesland FROM addresses GROUP BY 1 ORDER BY COUNT(*) DESC "
                       "LIMIT 1").fetchone()
    # change the state: one address less, one more in a new place
    conn.execute("DELETE FROM addresses WHERE rowid = (SELECT MIN(rowid) FROM addresses "
                 "WHERE bundesland = ?)", (bl,))
    conn.execute("INSERT INTO addresses (bundesland, kreis, gemeinde, plz, strasse, hausnummer, "
                 "hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm) "
                 "VALUES (?, 'Neukreis', 'Neudorf', '', 'Weg', '1', '', 25832, 1, 2, 'weg', 'neudorf')",
                 (bl,))
    lg.rebuild_lookup_tables(conn, bundeslaender=[bl])
    conn.commit()
    partial = _directory(conn)
    assert lg.lookup_tables_ready(conn)            # address count in lookup_meta is right
    lg.rebuild_lookup_tables(conn)
    conn.commit()
    assert _directory(conn) == partial
    conn.close()
