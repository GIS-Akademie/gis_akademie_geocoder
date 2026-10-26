# -*- coding: utf-8 -*-
"""
pytest fixtures for the GIS-Akademie Geocoder — they run WITHOUT QGIS.

* local_geocoder.py is QGIS-free and is tested directly.
* geocoder_worker.py only needs QObject/pyqtSignal from qgis.PyQt.QtCore;
  if QGIS is missing, a minimal stub is installed.
* The plugin folder is registered as the package 'gis_akademie_geocoder'
  so that the modules' relative imports work.
* The 'db' fixture builds a small SQLite database with synthetic but
  realistic addresses (places with the same name, districts, house number
  suffixes) including a municipality/district directory.

Usage (from the plugin folder):  python -m pytest tests -q
"""
import os
import sys
import types
import sqlite3

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_NAME   = 'gis_akademie_geocoder'


def _install_qt_stub():
    """qgis.PyQt.QtCore stub (QObject, pyqtSignal) if QGIS is missing —
    shared with tools/build_db.py in tools/_qt_stub.py."""
    sys.path.insert(0, os.path.join(PLUGIN_DIR, 'tools'))
    from _qt_stub import install_qt_stub
    install_qt_stub()


def _install_package():
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [PLUGIN_DIR]
        sys.modules[PKG_NAME] = pkg


_install_qt_stub()
_install_package()

import importlib  # noqa: E402
lg = importlib.import_module(f'{PKG_NAME}.local_geocoder')
gw = importlib.import_module(f'{PKG_NAME}.geocoder_worker')
pr = importlib.import_module(f'{PKG_NAME}.plz_resolver')

# The bundled postcode areas would assign real postcodes to test addresses
# (and loading takes a few seconds without QGIS). Off by default;
# test_plz_gebiete.py enables them selectively with small test polygons.
pr.set_enabled(False)


# ── Test data ────────────────────────────────────────────────────────────────
# (bundesland, kreis, gemeinde, plz, strasse, hausnummer, zusatz, epsg, x, y)
ADDRESSES = [
    # Frankfurt am Main (large) — Zeil only even numbers, Karl-Marx-Straße without no. 1
    *[('Hessen', 'Kreisfreie Stadt Frankfurt am Main', 'Frankfurt am Main', '60313',
       'Zeil', str(n), '', 25832, 477000 + n, 5551000) for n in (2, 4, 6, 8, 10, 12, 51)],
    ('Hessen', 'Kreisfreie Stadt Frankfurt am Main', 'Frankfurt am Main', '60386',
     'Karl-Marx-Straße', '3', '', 25832, 480100, 5552100),
    ('Hessen', 'Kreisfreie Stadt Frankfurt am Main', 'Frankfurt am Main', '60386',
     'Karl-Marx-Straße', '5', '', 25832, 480120, 5552110),
    ('Hessen', 'Kreisfreie Stadt Frankfurt am Main', 'Frankfurt am Main', '60385',
     'Karl-Albert-Straße', '1', '', 25832, 479000, 5552000),
    # Frankfurt (Oder) (small)
    ('Brandenburg', 'Frankfurt (Oder)', 'Frankfurt (Oder)', '15230',
     'Karl-Marx-Straße', '1', '', 25833, 470000, 5800000),
    ('Brandenburg', 'Frankfurt (Oder)', 'Frankfurt (Oder)', '15230',
     'Karl-Marx-Straße', '2', '', 25833, 470010, 5800010),
    ('Brandenburg', 'Frankfurt (Oder)', 'Frankfurt (Oder)', '15230',
     'Bahnhofstraße', '1', '', 25833, 470100, 5800100),
    # Halle (Saale) (large) / Halle (Westf.) (small)
    ('Sachsen-Anhalt', 'Halle (Saale), Stadt', 'Halle (Saale)', '06108',
     'Marktplatz', '1', '', 25832, 705000, 5705000),
    *[('Sachsen-Anhalt', 'Halle (Saale), Stadt', 'Halle (Saale)', '06132',
       'Bahnhofstraße', h, z, 25832, 705100 + i, 5705100) for i, (h, z) in
      enumerate([('1', ''), ('1', 'A'), ('2', ''), ('3', ''), ('4', '')])],
    ('Nordrhein-Westfalen', 'Gütersloh', 'Halle (Westf.)', '33790',
     'Bahnhofstraße', '1', '', 25832, 470500, 5760500),
    ('Nordrhein-Westfalen', 'Gütersloh', 'Halle (Westf.)', '33790',
     'Marktstraße', '1', '', 25832, 470520, 5760520),
    # Bochum
    ('Nordrhein-Westfalen', 'Bochum', 'Bochum', '44869', 'Elchweg', '3', '', 25832, 371339, 5701507),
    ('Nordrhein-Westfalen', 'Bochum', 'Bochum', '44869', 'Elchweg', '3', 'a', 25832, 371345, 5701510),
    ('Nordrhein-Westfalen', 'Bochum', 'Bochum', '44869', 'Elchweg', '4', '', 25832, 371350, 5701520),
    ('Nordrhein-Westfalen', 'Bochum', 'Bochum', '44869', 'Elchweg', '38', '', 25832, 371600, 5701700),
    ('Nordrhein-Westfalen', 'Bochum', 'Bochum', '44869', 'Efeuweg', '3', '', 25832, 371532, 5702189),
    # Hamburg with districts in 'kreis'
    ('Hamburg', 'Blankenese,OT 0217 (Blankenese)', 'Hamburg', '22587',
     'Blankeneser Hauptstraße', '1', '', 25832, 554000, 5936000),
    ('Hamburg', 'Blankenese,OT 0217 (Blankenese)', 'Hamburg', '22587',
     'Blankeneser Landstraße', '1', '', 25832, 554100, 5936100),
    ('Hamburg', 'Hamburg-Altstadt,OT 0101 (Hamburg-Altstadt)', 'Hamburg', '20354',
     'Jungfernstieg', '14', '', 25832, 565800, 5934500),
    ('Hamburg', 'Altona-Altstadt,OT 0201 (Altona-Altstadt)', 'Hamburg', '22767',
     'Große Bergstraße', '1', '', 25832, 562000, 5934000),
    # Berlin: boroughs in 'kreis', numbered streets
    ('Berlin', 'Pankow', 'Berlin', '13187', 'Breite Straße', '1', '', 25833, 392000, 5826000),
    ('Berlin', 'Mitte', 'Berlin', '10117', 'Unter den Linden', '1', '', 25833, 391500, 5819500),
    ('Berlin', 'Lichtenberg', 'Berlin', '13059', 'Straße 10', '5', '', 25833, 398000, 5826500),
    ('Berlin', 'Lichtenberg', 'Berlin', '13059', 'Straße 1', '10', '', 25833, 398100, 5826600),
    # München
    *[('Bayern', '', 'München', '81377', 'Lorettoplatz', str(n), '', 25832, 685200 + n * 5, 5330800)
      for n in (3, 5, 6, 7)],
    # Kassel: house number suffix
    ('Hessen', 'Kreisfreie Stadt Kassel', 'Kassel', '34117', 'Akazienweg', '3', 'A', 25832, 534215, 5685098),
    ('Hessen', 'Kreisfreie Stadt Kassel', 'Kassel', '34117', 'Akazienweg', '4', '', 25832, 534180, 5685102),
    ('Hessen', 'Kreisfreie Stadt Kassel', 'Kassel', '34117', 'Akazienweg', '7', '', 25832, 534093, 5685062),
    # Official abbreviation
    ('Hessen', 'Hersfeld-Rotenburg', 'Rotenburg a. d. Fulda', '36199', 'Marktplatz', '1', '', 25832, 548000, 5648000),
    # Short name vs. long form
    ('Niedersachsen', '', 'Oldenburg (Oldenb.)', '26122', 'Lange Straße', '1', '', 25832, 447000, 5884000),
    ('Niedersachsen', '', 'Oldenburg (Oldenb.)', '26122', 'Lange Straße', '2', '', 25832, 447010, 5884010),
    ('Schleswig-Holstein', 'Ostholstein', 'Oldenburg in Holstein', '23758', 'Lange Straße', '1', '', 25832, 608000, 6013000),
    # small town for the hyphen negative test
    ('Niedersachsen', 'Celle', 'Bergen', '29303', 'Hauptstraße', '1', '', 25832, 580000, 5845000),
]


@pytest.fixture
def db(tmp_path):
    """Small test DB including search indexes and municipality/district directory.
    Returns the local_geocoder module with DB_PATH set."""
    path = str(tmp_path / 'geocoder_test.db')
    conn = sqlite3.connect(path)
    lg.init_db(conn)
    rows = []
    for bl, kreis, gem, plz, strasse, hn, zusatz, epsg, x, y in ADDRESSES:
        rows.append((bl, kreis, gem, plz, strasse, hn, zusatz, epsg, float(x), float(y),
                     lg.normalize(strasse), lg._core_gemeinde(gem)))
    conn.executemany(
        "INSERT INTO addresses (bundesland, kreis, gemeinde, plz, strasse, hausnummer,"
        " hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    lg.set_db_path(path)
    lg.rebuild_lookup_tables(conn)
    conn.commit()
    conn.close()
    # The test DB is small: lower the threshold for 'city-district' (Berlin-Mitte).
    old = lg._MIN_HYPHEN_CITY_SIZE
    lg._MIN_HYPHEN_CITY_SIZE = 3
    yield lg
    lg._MIN_HYPHEN_CITY_SIZE = old


@pytest.fixture
def worker_mod(db):
    """geocoder_worker module (uses the same test DB)."""
    return gw
