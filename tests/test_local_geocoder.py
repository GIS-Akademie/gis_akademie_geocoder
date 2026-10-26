# -*- coding: utf-8 -*-
"""Tests for local_geocoder.py (without QGIS). See conftest.py for the test data."""
import pytest

from conftest import lg


# ── Normalization ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('Hauptstr.', 'hauptstraße'),
    ('Hauptstr', 'hauptstraße'),
    ('Bahnhofstr. ', 'bahnhofstraße'),
    ('Straße des 17. Juni', 'straße des 17 juni'),
    ('  Karl-Marx-Allee ', 'karl-marx-allee'),
    ('', ''),
    (None, ''),
])
def test_normalize(raw, expected):
    assert lg.normalize(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('12', '12'), ('12 a', '12a'), ('7A', '7a'),
    ('12-14', '12'), ('12 - 14', '12'), ('12/14', '12'), ('12+14', '12'),
    ('12a-c', '12a'), ('Nr. 7', '7'), ('Nr 7', '7'), ('', ''),
    # sub-number (second number not larger): a different building, kept
    ('3b/1', '3b/1'), ('12/1', '12/1'), ('12 / 1', '12/1'), ('1/1', '1/1'),
])
def test_normalize_hausnummer(raw, expected):
    assert lg.normalize_hausnummer(raw) == expected


@pytest.mark.parametrize('raw, street, hn', [
    ('Hauptstraße 12-14', 'hauptstraße', '12'),
    ('Hauptstr. 12 a', 'hauptstraße', '12a'),
    ('Straße des 17. Juni 135', 'straße des 17 juni', '135'),
    ('Elchweg 3-5', 'elchweg', '3'),
    ('Am Markt 1/2', 'am markt', '1'),
    ('Lindenstraße', 'lindenstraße', ''),
])
def test_extract_hausnummer(raw, street, hn):
    assert lg._extract_hausnummer(lg.normalize(raw)) == (street, hn)


@pytest.mark.parametrize('raw, expected', [
    ('Magdeburg, Landeshauptstadt', 'magdeburg'),
    ('Hanse- und Universitätsstadt Rostock', 'rostock'),
    ('Halle (Saale)', 'halle'),
    ('Lutherstadt Wittenberg', 'wittenberg'),
    # Known quirk: the status word 'stadt' is removed here as well. Since
    # DB import and search use the same function, the search stays consistent.
    ('Weil der Stadt', 'weil der'),
])
def test_core_gemeinde(raw, expected):
    assert lg._core_gemeinde(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('frankfurt am main', 'frankfurt'),
    ('bergen auf rügen', 'bergen'),
    ('bad homburg vor der höhe', 'bad homburg'),
    ('rotenburg an der fulda', 'rotenburg'),
    ('neustadt in holstein', 'neustadt'),
    ('mühlhausen/thüringen', 'mühlhausen'),
    ('weil der stadt', 'weil der stadt'),
    ('halle', 'halle'),
])
def test_kurz_norm(raw, expected):
    assert lg._kurz_norm(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('Rahlstedt,OT 0526 (Rahlstedt)', 'rahlstedt'),
    ('Kreisfreie Stadt Kassel', 'kassel'),
    ('Friedrichshain-Kreuzberg', 'friedrichshain-kreuzberg'),
    ('Landkreis Celle', 'celle'),
])
def test_ortsteil_alias_norm(raw, expected):
    assert lg._ortsteil_alias_norm(raw) == expected


# ── Bundesland detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('NRW', 'Nordrhein-Westfalen'), ('Nordrhein Westfalen', 'Nordrhein-Westfalen'),
    ('Nordrhein-Westfalen', 'Nordrhein-Westfalen'), ('Freistaat Bayern', 'Bayern'),
    ('BW', 'Baden-Württemberg'), ('Sachsen', 'Sachsen'), ('Niedersachsen', 'Niedersachsen'),
    ('Deutschland', ''), ('Germany', ''), ('xyz', ''), ('', ''),
])
def test_canonical_bundesland(raw, expected):
    assert lg.canonical_bundesland(raw) == expected


@pytest.mark.parametrize('plz, expected', [
    ('44869', 'Nordrhein-Westfalen'), ('34117', 'Hessen'), ('81377', 'Bayern'),
    ('37073', ''),      # Göttingen/Eschwege/Beverungen: deliberately unassigned
    ('76133', ''),      # Karlsruhe/Landau: deliberately unassigned
    ('1', ''), ('', ''), (None, ''),
])
def test_bundesland_from_plz(plz, expected):
    assert lg.bundesland_from_plz(plz) == expected


def test_plz_prefix_table_is_unambiguous():
    seen = {}
    for bl, prefixes in lg._PLZ_PREFIX_GROUPS.items():
        for p in prefixes:
            assert p not in seen, f'Präfix {p} bei {bl} und {seen[p]}'
            seen[p] = bl


# ── Directory ────────────────────────────────────────────────────────────────

def test_lookup_tables_built(db):
    conn = db.get_connection()
    try:
        assert db.lookup_tables_ready(conn)
        row = conn.execute(
            "SELECT kurz_norm, bundesland, n FROM gemeinde_index "
            "WHERE gemeinde_norm = 'frankfurt am main'").fetchone()
        assert (row['kurz_norm'], row['bundesland']) == ('frankfurt', 'Hessen')
        assert row['n'] == 10
        aliases = {r[0] for r in conn.execute("SELECT alias_norm FROM ortsteil_index")}
        assert {'blankenese', 'pankow', 'mitte', 'altona-altstadt'} <= aliases
        assert 'bochum' not in aliases        # kreis == gemeinde: no alias
    finally:
        conn.close()


def test_lookup_tables_stale_after_insert(db):
    conn = db.get_connection()
    conn.execute("INSERT INTO addresses (bundesland, strasse, x, y) VALUES ('Hessen', 'Neu', 0, 0)")
    conn.commit()
    assert not db.lookup_tables_ready(conn)
    conn.close()


def test_gemeinde_targets_order(db):
    conn = db.get_connection()
    try:
        t = db._gemeinde_targets(conn, 'frankfurt', None, raw='Frankfurt')
        assert [x[0] for x in t] == ['frankfurt am main', 'frankfurt']   # largest first
        t = db._gemeinde_targets(conn, 'frankfurt', None, raw='Frankfurt (Oder)')
        assert t[0][:2] == ('frankfurt', 'Brandenburg')                   # suffix wins
        t = db._gemeinde_targets(conn, 'frankfurt', None, raw='Frankfurt', plz='15230')
        assert t[0][:2] == ('frankfurt', 'Brandenburg')                   # postcode wins
        t = db._gemeinde_targets(conn, 'halle', 'Nordrhein-Westfalen', raw='Halle')
        assert t[0][:2] == ('halle', 'Nordrhein-Westfalen')               # Bundesland wins
        assert db._gemeinde_targets(conn, 'blankenese', None)[0][:2] == ('hamburg', 'Hamburg')
        assert db._gemeinde_targets(conn, 'pankow', None)[0][:2] == ('berlin', 'Berlin')
        assert db._gemeinde_targets(conn, 'berlin-mitte', None)[0][:2] == ('berlin', 'Berlin')
        assert db._gemeinde_targets(conn, 'bergen-enkheim', None) == []  # Bergen too small
        assert db._gemeinde_targets(conn, 'nirgendwo', None) == []
        assert db._gemeinde_targets(conn, 'oldenburg', None)[0][0] == 'oldenburg'
        assert db._gemeinde_targets(conn, 'rotenburg a. d. fulda', None)[0][0] == 'rotenburg a d fulda'
    finally:
        conn.close()


# ── Search ───────────────────────────────────────────────────────────────────

def _top(db, **kw):
    hits = db.search(max_results=3, **kw)
    assert hits, f'kein Treffer für {kw}'
    return hits[0]


def test_search_exact(db):
    h = _top(db, strasse='Elchweg 3', plz='44869', gemeinde='Bochum')
    assert (h['strasse'], h['hausnummer'], h['hn_score'], h['score_label']) == ('Elchweg', '3', 1.0, 'Exakt')


def test_hausnummer_bonus_only_for_letter_suffix(db):
    hits = db.search(strasse='Zeil 1', gemeinde='Frankfurt am Main', max_results=5)
    assert all(h['hn_score'] == 0.0 for h in hits)          # 10, 12 are DIFFERENT numbers
    hits = db.search(strasse='Akazienweg 3', gemeinde='Kassel', max_results=3)
    assert hits[0]['hausnummer'] == '3A' and hits[0]['hn_score'] == 0.5


def test_ambiguous_city_prefers_largest_with_hint(db):
    h = _top(db, strasse='Zeil 2', gemeinde='Frankfurt')
    assert h['gemeinde'] == 'Frankfurt am Main'
    assert h['hinweis'].startswith('Ort mehrdeutig (2 Orte)')


def test_city_suffix_disambiguates_without_hint(db):
    h = _top(db, strasse='Karl-Marx-Straße 1', gemeinde='Frankfurt (Oder)')
    assert (h['gemeinde'], h['hausnummer'], h['hinweis']) == ('Frankfurt (Oder)', '1', '')
    h = _top(db, strasse='Karl-Marx-Straße 1', gemeinde='Frankfurt/Oder')
    assert h['gemeinde'] == 'Frankfurt (Oder)'


def test_plz_disambiguates(db):
    h = _top(db, strasse='Bahnhofstraße 1', gemeinde='Halle', plz='33790')
    assert (h['gemeinde'], h['plz'], h['hinweis']) == ('Halle (Westf.)', '33790', '')
    h = _top(db, strasse='Karl-Marx-Straße 1', gemeinde='Frankfurt', plz='15230')
    assert h['gemeinde'] == 'Frankfurt (Oder)'


def test_bundesland_restricts_city_choice(db):
    # Halle (Westf.) has no Marktplatz — still do NOT fall back to Halle (Saale)
    h = _top(db, strasse='Marktplatz 1', gemeinde='Halle', bundesland='Nordrhein-Westfalen')
    assert h['bundesland'] == 'Nordrhein-Westfalen'
    assert h['score'] < 0.75                                 # honest: only a fuzzy hit


def test_hausnummer_bonus_gated_by_street_quality(db):
    # FFM has Karl-Marx-Straße (without no. 1) and Karl-Albert-Straße 1
    h = _top(db, strasse='Karl-Marx-Straße 1', gemeinde='Frankfurt am Main')
    assert h['strasse'] == 'Karl-Marx-Straße'


def test_ortsteil_resolves_to_city(db):
    h = _top(db, strasse='Blankeneser Hauptstraße 1', gemeinde='Blankenese')
    assert (h['gemeinde'], h['hausnummer'], h['score_label']) == ('Hamburg', '1', 'Exakt')
    h = _top(db, strasse='Breite Straße 1', gemeinde='Pankow')
    assert h['gemeinde'] == 'Berlin'


def test_city_dash_district(db):
    h = _top(db, strasse='Unter den Linden 1', gemeinde='Berlin-Mitte')
    assert h['gemeinde'] == 'Berlin' and h['hausnummer'] == '1'
    h = _top(db, strasse='Große Bergstraße 1', gemeinde='Hamburg-Altona')
    assert h['gemeinde'] == 'Hamburg'


def test_separate_hausnummer_keeps_numbered_street(db):
    h = _top(db, strasse='Straße 10', hausnummer='5', gemeinde='Berlin')
    assert (h['strasse'], h['hausnummer']) == ('Straße 10', '5')
    # without a separate column, the 10 is read as house number (as documented)
    h = _top(db, strasse='Straße 10', gemeinde='Berlin')
    assert (h['strasse'], h['hausnummer']) == ('Straße 1', '10')


def test_hausnummer_ranges_and_duplicates(db):
    h = _top(db, strasse='Elchweg 3-5', gemeinde='Bochum')
    assert (h['hausnummer'], h['hn_score']) == ('3', 1.0)
    h = _top(db, strasse='Elchweg 3', hausnummer='3', gemeinde='Bochum')   # number given twice
    assert (h['strasse'], h['hausnummer'], h['hn_score']) == ('Elchweg', '3', 1.0)
    h = _top(db, strasse='Elchweg', hausnummer='3 a', gemeinde='Bochum')
    assert (h['hausnummer'], h['hn_score']) == ('3a', 1.0)


def test_official_abbreviation(db):
    h = _top(db, strasse='Marktplatz 1', gemeinde='Rotenburg an der Fulda')
    assert h['gemeinde'] == 'Rotenburg a. d. Fulda' and h['score_label'] == 'Exakt'


def test_short_name_finds_long_form(db):
    h = _top(db, strasse='Lange Straße 1', gemeinde='Oldenburg')
    assert h['gemeinde'] == 'Oldenburg (Oldenb.)'
    h = _top(db, strasse='Lange Straße 1', gemeinde='Oldenburg in Holstein')
    assert h['gemeinde'] == 'Oldenburg in Holstein'


def test_plz_only_without_city(db):
    h = _top(db, strasse='Lorettoplatz 7', plz='81377')
    assert h['gemeinde'] == 'München' and h['hn_score'] == 1.0


def test_wrong_city_right_plz_still_found(db):
    h = _top(db, strasse='Lorettoplatz 7', plz='81377', gemeinde='Muenchn')
    assert h['gemeinde'] == 'München'


def test_unknown_street_returns_nothing(db):
    assert db.search(strasse='Gibtsnichtweg 1', gemeinde='Bochum') == []


def test_search_result_carries_norm_fields(db):
    h = _top(db, strasse='Elchweg 3', gemeinde='Bochum')
    assert (h['strasse_norm'], h['gemeinde_norm']) == ('elchweg', 'bochum')


# ── Street representative / locality centroid / diagnosis ────────────────────

def test_street_representative_nearest_and_center(db):
    rp = db.street_representative('lorettoplatz', 'münchen', 'Bayern', hausnummer='999')
    assert (rp['kind'], rp['hausnummer']) == ('nearest', '7')
    rp = db.street_representative('lorettoplatz', 'münchen', 'Bayern', hausnummer='4')
    assert rp['hausnummer'] == '3'                           # tie 3/5 → smaller one
    rp = db.street_representative('lorettoplatz', 'münchen', 'Bayern')
    assert rp['kind'] == 'center' and rp['count'] == 4
    assert db.street_representative('gibtsnicht', 'münchen') is None


def test_locate_locality(db):
    loc = db.locate_locality(plz='81377')
    assert loc['label'].startswith('PLZ') and loc['bundesland'] == 'Bayern'
    loc = db.locate_locality(gemeinde='Blankenese')
    assert loc['bundesland'] == 'Hamburg' and loc['level'] == 2
    loc = db.locate_locality(gemeinde='Frankfurt')
    assert loc['bundesland'] == 'Hessen'
    assert db.locate_locality(gemeinde='Nirgendwo') is None


def test_diagnose_search(db):
    assert db.diagnose_search('Gibtsnichtweg 1', gemeinde='Blankenese') == \
        'Straße „Gibtsnichtweg 1“ in Blankenese nicht gefunden'
    assert db.diagnose_search('Elchweg 1', gemeinde='Nirgendwo').startswith(
        'Ort „Nirgendwo“ ist in der lokalen Datenbank nicht enthalten')
    assert db.diagnose_search('Elchweg 1', gemeinde='Bochum', bundesland='Saarland') == \
        'Bundesland Saarland ist in der lokalen Datenbank nicht enthalten'


# ── Fallback without directory / path change ─────────────────────────────────

def test_search_works_without_lookup_tables(db):
    conn = db.get_connection()
    for t in ('gemeinde_index', 'ortsteil_index', 'lookup_meta'):
        conn.execute(f'DROP TABLE {t}')
    conn.commit()
    conn.close()
    db._lookup_available.clear()
    h = _top(db, strasse='Elchweg 3', gemeinde='Bochum')
    assert h['hausnummer'] == '3'
    # old logic: short name cannot be resolved, but no error
    assert db.search(strasse='Zeil 2', gemeinde='Frankfurt') == []


def test_set_db_path_resets_caches(db, tmp_path):
    db._indexes_ensured = True
    db._lookup_checked = True
    db.set_db_path(str(tmp_path / 'andere.db'))
    assert db._indexes_ensured is False and db._lookup_checked is False
    assert db._lookup_available == {}
    assert db.db_exists() is False
