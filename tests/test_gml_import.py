# -*- coding: utf-8 -*-
"""GML import: file lists (WFS pages), wfs:member, municipality via INSPIRE level."""
import importlib

from conftest import PKG_NAME
gml = importlib.import_module(f'{PKG_NAME}.gml_import_worker')

NS = ('xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" '
      'xmlns:ad="http://inspire.ec.europa.eu/schemas/ad/4.0" xmlns:gn="http://inspire.ec.europa.eu/schemas/gn/4.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink"')


def _name(tag, gid, text, level=None):
    lvl = (f'<ad:level xlink:href="http://inspire.ec.europa.eu/codelist/AdministrativeHierarchyLevel/{level}"/>'
           if level else '')
    return (f'<wfs:member><ad:{tag} gml:id="{gid}"><ad:name><gn:GeographicalName><gn:spelling>'
            f'<gn:SpellingOfName><gn:text>{text}</gn:text></gn:SpellingOfName></gn:spelling>'
            f'</gn:GeographicalName></ad:name>{lvl}</ad:{tag}></wfs:member>')


WFS_REF = ('https://geodienste.hamburg.de/HH_WFS_INSPIRE_Adressen?SERVICE=WFS&amp;VERSION=2.0.0'
           '&amp;REQUEST=GetFeature&amp;STOREDQUERY_ID=urn:ogc:def:query:OGC-WFS::GetFeatureById&amp;ID={id}#{id}')


def _address(gid, hn, x, y, refs, url_refs=False, epsg=25833):
    comps = ''.join(f'<ad:component xlink:href="{WFS_REF.format(id=r) if url_refs else "#" + r}"/>'
                    for r in refs)
    return (f'<wfs:member><ad:Address gml:id="{gid}"><ad:position><ad:GeographicPosition><ad:geometry>'
            f'<gml:Point gml:id="P{gid}" srsName="urn:ogc:def:crs:EPSG::{epsg}"><gml:pos>{x} {y}</gml:pos></gml:Point>'
            f'</ad:geometry></ad:GeographicPosition></ad:position>'
            f'<ad:locator><ad:AddressLocator><ad:designator><ad:LocatorDesignator>'
            f'<ad:designator>{hn}</ad:designator>'
            f'<ad:type xlink:href="http://inspire.ec.europa.eu/codelist/LocatorDesignatorTypeValue/addressNumber"/>'
            f'</ad:LocatorDesignator></ad:designator></ad:AddressLocator></ad:locator>'
            f'{comps}</ad:Address></wfs:member>')


def _collection(members):
    return f'<wfs:FeatureCollection {NS}>{"".join(members)}</wfs:FeatureCollection>'


def test_gml_import_wfs_pages_with_level(db, tmp_path):
    f_names = tmp_path / 'ad_AdminUnitName_0000.gml'
    f_names.write_text(_collection([
        _name('AdminUnitName', 'AdminUnitName_L', 'Mecklenburg-Vorpommern', '1stOrder'),
        _name('AdminUnitName', 'AdminUnitName_K', 'Landkreis Rostock', '2ndOrder'),
        _name('AdminUnitName', 'AdminUnitName_G', 'Güstrow, Barlachstadt', '4thOrder'),
        _name('AdminUnitName', 'AdminUnitName_O', 'Suckow', '5thOrder'),
    ]), encoding='utf-8')
    f_streets = tmp_path / 'ad_ThoroughfareName_0000.gml'
    f_streets.write_text(_collection([_name('ThoroughfareName', 'ThoroughfareName_S', 'Lange Str.')]), encoding='utf-8')
    f_addr = tmp_path / 'ad_Address_0000.gml'
    f_addr.write_text(_collection([
        _address('A1', '7', 300000.0, 5970000.0,
                 ['AdminUnitName_L', 'AdminUnitName_K', 'AdminUnitName_G', 'AdminUnitName_O', 'ThoroughfareName_S']),
        _address('A2', '9', 300010.0, 5970010.0,
                 ['AdminUnitName_L', 'AdminUnitName_K', 'AdminUnitName_G', 'ThoroughfareName_S']),
    ]), encoding='utf-8')

    w = gml.GmlImportWorker([str(f_names), str(f_streets), str(f_addr)],
                            'Mecklenburg-Vorpommern', replace=True, gemeinde_level='4thOrder')
    result, logs = {}, []
    w.log.connect(logs.append)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], (result, logs)

    conn = db.get_connection()
    rows = conn.execute("SELECT gemeinde, gemeinde_norm, kreis, strasse, strasse_norm, hausnummer, epsg, plz "
                        "FROM addresses WHERE bundesland='Mecklenburg-Vorpommern' ORDER BY hausnummer").fetchall()
    conn.close()
    assert len(rows) == 2
    g, gn, kreis, strasse, sn, hn, epsg, plz = rows[0]
    assert (g, gn) == ('Güstrow, Barlachstadt', 'güstrow')      # level 4 = municipality, status suffix removed
    assert kreis == 'Suckow'                                     # last unit = district
    assert (strasse, sn, hn, epsg, plz) == ('Lange Str.', 'lange straße', '7', 25833, '')
    assert rows[1][2] == 'Güstrow, Barlachstadt'                 # no district: last = municipality


def test_gml_import_without_level_uses_second_unit(db, tmp_path):
    f = tmp_path / 'hh.gml'
    f.write_text(_collection([
        _name('AdminUnitName', 'AdminUnitName_1', 'Freie und Hansestadt Hamburg'),
        _name('AdminUnitName', 'AdminUnitName_2', 'Hamburg'),
        _name('AdminUnitName', 'AdminUnitName_3', 'Altona'),
        _name('ThoroughfareName', 'ThoroughfareName_S', 'Große Bergstraße'),
        _address('A1', '1', 562000.0, 5934000.0,
                 ['AdminUnitName_1', 'AdminUnitName_2', 'AdminUnitName_3', 'ThoroughfareName_S', 'PostalDescriptor_22767']),
    ]), encoding='utf-8')
    w = gml.GmlImportWorker(str(f), 'Hamburg', replace=True)
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    conn = db.get_connection()
    row = conn.execute("SELECT gemeinde, kreis, plz FROM addresses WHERE bundesland='Hamburg'").fetchone()
    conn.close()
    assert (row[0], row[1], row[2]) == ('Hamburg', 'Altona', '22767')   # 2nd unit = city, postcode from PostalDescriptor


def test_gml_import_hamburg_wfs_structure(db, tmp_path):
    """Structure like the Hamburg WFS (geodienste.hamburg.de): references as full
    GetFeatureById URLs, levels 1–6, postcode in the PostalDescriptor reference."""
    names = tmp_path / 'ad_AdminUnitName_0000.gml'
    names.write_text(_collection([
        _name('AdminUnitName', 'AdminUnitName_49000000000000', 'Germany', '1stOrder'),
        _name('AdminUnitName', 'AdminUnitName_49020000000000', 'Hamburg', '2ndOrder'),
        _name('AdminUnitName', 'AdminUnitName_49026000000000', 'Bergedorf', '3rdOrder'),
        _name('AdminUnitName', 'AdminUnitName_49026010000000', 'Lohbrügge', '4thOrder'),
        _name('AdminUnitName', 'AdminUnitName_49026016010000', 'Lohbrügge', '5thOrder'),
        _name('AdminUnitName', 'AdminUnitName_49026016010601', 'Lohbrügge,OT 0601 (Lohbrügge)', '6thOrder'),
    ]), encoding='utf-8')
    streets = tmp_path / 'ad_ThoroughfareName_0000.gml'
    streets.write_text(_collection([_name('ThoroughfareName', 'ThoroughfareName_A1240', 'Lohbrügger Markt')]),
                       encoding='utf-8')
    refs = ['AdminUnitName_49000000000000', 'AdminUnitName_49020000000000', 'AdminUnitName_49026000000000',
            'AdminUnitName_49026010000000', 'AdminUnitName_49026016010000', 'AdminUnitName_49026016010601',
            'ThoroughfareName_A1240', 'PostalDescriptor_21031']
    addr = tmp_path / 'ad_Address_0000.gml'
    addr.write_text(_collection([_address('Address_1', '1', 574413.5, 5932015.0, refs, url_refs=True, epsg=25832)]),
                    encoding='utf-8')
    w = gml.GmlImportWorker([str(names), str(streets), str(addr)], 'Hamburg', replace=True,
                            gemeinde_level='2ndOrder')
    result = {}
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    assert result['ok'], result
    conn = db.get_connection()
    row = conn.execute("SELECT gemeinde, kreis, plz, strasse, hausnummer, epsg FROM addresses "
                       "WHERE bundesland='Hamburg'").fetchone()
    conn.close()
    assert tuple(row) == ('Hamburg', 'Lohbrügge,OT 0601 (Lohbrügge)', '21031', 'Lohbrügger Markt', '1', 25832)
