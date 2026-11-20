# -*- coding: utf-8 -*-
"""Drag & drop onto the path fields: choosing the file from a drop."""
import os

from gis_akademie_geocoder import drop_paths as dp


def test_plain_path_is_kept():
    p = os.path.join('C:' + os.sep, 'Daten', 'adressen.csv')
    assert dp.pick_path([p], dp.INPUT_EXTS) == os.path.normpath(p)


def test_extension_is_case_insensitive():
    assert dp.pick_path(['liste.XLSX'], dp.INPUT_EXTS) == 'liste.XLSX'


def test_first_suitable_file_wins():
    got = dp.pick_path(['ordner', 'bild.png', 'a.ods', 'b.csv'], dp.INPUT_EXTS)
    assert got == 'a.ods'


def test_unsuitable_drop_gives_empty():
    assert dp.pick_path(['bild.png', 'ordner'], dp.INPUT_EXTS) == ''
    assert dp.pick_path([], dp.GPKG_EXTS) == ''
    assert dp.pick_path(None, dp.GPKG_EXTS) == ''
    assert dp.pick_path(['adressen.csv'], dp.GPKG_EXTS) == ''


def test_file_url_is_decoded():
    got = dp.to_local_path('file:///C:/Meine%20Daten/Stra%C3%9Fen.csv')
    assert got.replace(os.sep, '/').endswith('C:/Meine Daten/Straßen.csv')


def test_unc_url_keeps_the_host():
    got = dp.to_local_path('file://server/freigabe/geocoder.db')
    assert got.replace(os.sep, '/') == '//server/freigabe/geocoder.db'


def test_qgis_layer_options_are_cut_off():
    assert dp.pick_path(['C:/d/ergebnis.gpkg|layername=adressen'], dp.GPKG_EXTS) \
        == os.path.normpath('C:/d/ergebnis.gpkg')
    got = dp.pick_path(['file:///C:/d/liste.csv?delimiter=;&type=csv'], dp.INPUT_EXTS)
    assert got.replace(os.sep, '/').endswith('C:/d/liste.csv')


def test_remote_sources_are_ignored():
    assert dp.to_local_path('https://example.org/a.csv') == ''
    assert dp.to_local_path('') == ''
    assert dp.to_local_path(None) == ''
