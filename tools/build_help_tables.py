# -*- coding: utf-8 -*-
"""
Generates the source and license tables in help.html from sources.json and the
changelog in help.html and README.md from metadata.txt — so that the help, the
GitHub page and the plugin manager do not drift apart.

The tables are located in help.html between markers:
    <!-- TABELLE:quellen -->     … <!-- /TABELLE:quellen -->
    <!-- TABELLE:abruf -->       … <!-- /TABELLE:abruf -->
    <!-- TABELLE:laender -->     … <!-- /TABELLE:laender -->
    <!-- TABELLE:vermerke -->    … <!-- /TABELLE:vermerke -->
    <!-- TABELLE:aenderungen --> … <!-- /TABELLE:aenderungen -->   (changelog)
and in README.md between <!-- AENDERUNGEN --> … <!-- /AENDERUNGEN -->.

Usage (after every change to sources.json or to the changelog in metadata.txt):
    python tools/build_help_tables.py           # update help.html and README.md
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
README = os.path.join(PLUGIN_DIR, 'README.md')
CATALOG = os.path.join(PLUGIN_DIR, 'sources.json')
METADATA = os.path.join(PLUGIN_DIR, 'metadata.txt')

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


def changelog(path=METADATA):
    """Changelog entries [(version, text)] from metadata.txt, newest first as written there.
    Read from the raw text (configparser would drop the indentation): an entry
    starts with '<version> - ' at the indentation of the first entry; lines
    indented deeper continue the previous entry — so a wrapped line that begins
    with a number ("2 - 3 Minuten") is never taken for a new version."""
    lines = open(path, encoding='utf-8').read().splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if re.match(r'changelog\s*=', ln))
    except StopIteration:
        return []
    first = lines[start].split('=', 1)[1].strip()
    block = ([first] if first else []) + [ln for ln in
                                          __import__('itertools').takewhile(
                                              lambda ln: not ln.strip() or ln[:1] in ' \t',
                                              lines[start + 1:])]
    indents = [len(ln) - len(ln.lstrip()) for ln in block if ln.strip()]
    base = min(indents) if indents else 0
    entries = []
    for ln in block:
        if not ln.strip():
            continue
        indent = len(ln) - len(ln.lstrip())
        m = re.match(r'(\d[\w.]*)\s+-\s+(.*)', ln.strip())
        if m and indent <= base:
            entries.append([m.group(1), m.group(2)])
        elif entries:
            entries[-1][1] += ' ' + ln.strip()
    return [tuple(e) for e in entries]


def _changelog_html(entries):
    return ['    <table>', '      <tr><th>Version</th><th>Änderungen</th></tr>',
            *(f'      <tr><td>{_e(v)}</td><td>{_e(t)}</td></tr>' for v, t in entries),
            '    </table>']


def _replace(text, start, end, block, name):
    pat = re.compile(re.escape(start) + '.*?' + re.escape(end), re.S)
    if not pat.search(text):
        raise SystemExit(f'Markierung {start} fehlt in {name}')
    return pat.sub(lambda m: block, text)


def render(text, cat, entries=None):
    blocks = tables(cat)
    blocks['aenderungen'] = _changelog_html(changelog() if entries is None else entries)
    for key, lines in blocks.items():
        block = f'<!-- TABELLE:{key} -->\n' + '\n'.join(lines) + f'\n    <!-- /TABELLE:{key} -->'
        text = _replace(text, f'<!-- TABELLE:{key} -->', f'<!-- /TABELLE:{key} -->', block, 'help.html')
    return text


def render_readme(text, entries=None):
    entries = changelog() if entries is None else entries
    block = '\n'.join(['<!-- AENDERUNGEN -->', *(f'- **{v}** – {t}' for v, t in entries),
                       '<!-- /AENDERUNGEN -->'])
    return _replace(text, '<!-- AENDERUNGEN -->', '<!-- /AENDERUNGEN -->', block, 'README.md')


def main():
    ap = argparse.ArgumentParser(description='Quellentabellen und Änderungsprotokoll in Hilfe und README erzeugen')
    ap.add_argument('--check', action='store_true', help='nur prüfen, nichts schreiben')
    args = ap.parse_args()
    cat = json.load(open(CATALOG, encoding='utf-8'))
    entries = changelog()
    outdated = []
    for path, build in ((HELP, lambda t: render(t, cat, entries)),
                        (README, lambda t: render_readme(t, entries))):
        name = os.path.basename(path)
        old = open(path, encoding='utf-8').read()
        new = build(old)
        if new == old:
            print(f'{name} war bereits aktuell.' if not args.check else f'{name} ist aktuell.')
        elif args.check:
            outdated.append(name)
        else:
            open(path, 'w', encoding='utf-8', newline='').write(new)
            print(f'{name} aktualisiert.')
    if outdated:
        print(', '.join(outdated) + ' veraltet — bitte tools/build_help_tables.py ausführen.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
