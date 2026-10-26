# -*- coding: utf-8 -*-
"""
Generates the source and license tables in help.html from sources.json —
so that the help and the source catalog do not drift apart.

The tables are located in help.html between markers:
    <!-- TABELLE:quellen -->     … <!-- /TABELLE:quellen -->
    <!-- TABELLE:abruf -->       … <!-- /TABELLE:abruf -->
    <!-- TABELLE:laender -->     … <!-- /TABELLE:laender -->
    <!-- TABELLE:vermerke -->    … <!-- /TABELLE:vermerke -->

Usage (after every change to sources.json):
    python tools/build_help_tables.py           # update help.html
    python tools/build_help_tables.py --check   # check only (returns 1 if outdated)
"""
import os
import re
import sys
import html
import json
import argparse
import urllib.parse

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELP = os.path.join(PLUGIN_DIR, 'help.html')
CATALOG = os.path.join(PLUGIN_DIR, 'sources.json')

ORDER = ('Baden-Württemberg', 'Bayern', 'Berlin', 'Brandenburg', 'Bremen', 'Hamburg', 'Hessen',
         'Mecklenburg-Vorpommern', 'Niedersachsen', 'Nordrhein-Westfalen', 'Rheinland-Pfalz',
         'Saarland', 'Sachsen', 'Sachsen-Anhalt', 'Schleswig-Holstein', 'Thüringen')

ZUGRIFF = {
    'direct': 'feste Datei-URL', 'index_json': 'Verzeichnisindex', 'page_link': 'Link auf der Downloadseite',
    'atom': 'Atom-Feed', 'rss': 'RSS-Feed', 'ckan': 'CKAN-Datenkatalog', 'wfs': 'INSPIRE-WFS',
    'index_geojson': 'Dateiliste je Kreis', 'gaialight': 'Downloaddienst (Auftrag)',
    'ogcapi': 'OGC API Features', 'manuell': 'nur manuell',
}
FORMAT = {
    'hkde': 'HK-DE-Text', 'gml': 'INSPIRE-GML', 'shp': 'Shapefile', 'osm': 'OpenStreetMap (.osm.pbf)',
    'alkis': 'ALKIS-GeoPackage', 'geojson': 'GeoJSON',
}


def _e(s):
    return html.escape(str(s or ''), quote=False)


def _host(url):
    return urllib.parse.urlparse(url or '').netloc


def tables(cat):
    src = cat['sources']
    rows_q, rows_a, rows_l, rows_v = [], [], [], []
    for bl in ORDER:
        e = src.get(bl) or {}
        url = e.get('url', '')
        name = _e(e.get('quelle_name', ''))
        link = f'<a href="{_e(url)}" target="_blank">{name}</a>' if url else name
        rows_q.append(f'      <tr><td>{bl}</td><td>{link}</td><td>{_e(e.get("lizenz", ""))}</td></tr>')
        zugriff = f'{_e(_host(url))}, {ZUGRIFF.get(e.get("typ"), e.get("typ", ""))}'
        if e.get('ausweich'):
            zugriff += f'; Ausweichquelle: {_e(_host(e["ausweich"].get("url")))}'
        hinweis = _e(e.get('hinweis', ''))
        if e.get('dauer') and e.get('dauer') != 'DAUER':
            hinweis += f' Abruf: {_e(e["dauer"])}.'
        rows_a.append(f'      <tr><td>{bl}</td><td>{zugriff}</td>'
                      f'<td>{FORMAT.get(e.get("format"), e.get("format", ""))}</td><td>{hinweis}</td></tr>')
        plz = 'aus der Quelle' if e.get('liefert_plz') else 'ergänzt aus den PLZ-Gebieten'
        amtlich = '' if e.get('format') != 'osm' else ' (nicht amtlich)'
        status = '✅ automatisch' if e.get('typ') != 'manuell' else '⚠ manuell (Datei eintragen)'
        rows_l.append(f'      <tr><td>{bl}</td><td>{FORMAT.get(e.get("format"), "")}{amtlich}</td>'
                      f'<td>{plz}</td><td>{status}</td></tr>')
        rows_v.append(f'      <tr><td>{bl}</td><td>{_e(e.get("vermerk", e.get("lizenz", "")))}</td></tr>')
    rows_v.append('      <tr><td>PLZ-Gebiete (mitgeliefert)</td><td>© OpenStreetMap-Mitwirkende, ODbL 1.0</td></tr>')
    return {
        'quellen': ['    <table>', '      <tr><th>Bundesland</th><th>Quelle</th><th>Lizenz</th></tr>',
                    *rows_q, '    </table>'],
        'abruf': ['    <table>', '      <tr><th>Bundesland</th><th>Zugriff</th><th>Format</th><th>Hinweis</th></tr>',
                  *rows_a, '    </table>'],
        'laender': ['    <table>', '      <tr><th>Bundesland</th><th>Daten</th><th>Postleitzahlen</th><th>Abruf</th></tr>',
                    *rows_l, '    </table>'],
        'vermerke': ['    <table>', '      <tr><th>Bundesland</th><th>Quellenvermerk</th></tr>',
                     *rows_v, '    </table>'],
    }


def render(text, cat):
    for key, lines in tables(cat).items():
        pat = re.compile(rf'<!-- TABELLE:{key} -->.*?<!-- /TABELLE:{key} -->', re.S)
        if not pat.search(text):
            raise SystemExit(f'Markierung TABELLE:{key} fehlt in help.html')
        block = f'<!-- TABELLE:{key} -->\n' + '\n'.join(lines) + f'\n    <!-- /TABELLE:{key} -->'
        text = pat.sub(lambda m: block, text)
    return text


def main():
    ap = argparse.ArgumentParser(description='Quellentabellen der Hilfe aus sources.json erzeugen')
    ap.add_argument('--check', action='store_true', help='nur prüfen, nichts schreiben')
    args = ap.parse_args()
    cat = json.load(open(CATALOG, encoding='utf-8'))
    old = open(HELP, encoding='utf-8').read()
    new = render(old, cat)
    if args.check:
        if new != old:
            print('help.html ist veraltet — bitte tools/build_help_tables.py ausführen.')
            return 1
        print('help.html passt zu sources.json.')
        return 0
    if new != old:
        open(HELP, 'w', encoding='utf-8', newline='').write(new)
        print('help.html aktualisiert.')
    else:
        print('help.html war bereits aktuell.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
