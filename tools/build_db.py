# -*- coding: utf-8 -*-
"""
Build or update the geocoder database without QGIS — from all
automatically retrievable open data sources (catalog: sources.json).

Intended for GIS-Akademie, which distributes the finished database: one run
fetches NRW, Berlin, Sachsen, Mecklenburg-Vorpommern (WFS) and Hamburg
(experimental), imports them and builds the municipality/district directory.
States with a shop/registration/bot protection are skipped and listed —
they still have to be added in the plugin (Admin tab) or via script.

Usage (from the plugin folder or anywhere):
    python tools/build_db.py C:/Daten/geocoder.db                # all automatic sources
    python tools/build_db.py C:/Daten/geocoder.db --bundesland Berlin Sachsen
    python tools/build_db.py C:/Daten/geocoder.db --force        # even if the source is unchanged
    python tools/build_db.py --list                               # show catalog

Limitation: filling in postcodes from a polygon layer needs QGIS (plz_resolver)
and is not available here — NRW/MV therefore stay without postcodes until they
are imported in the plugin with a postcode layer. Everything else (normalization,
directory, indexes) is identical to the import in the plugin.
"""
import os
import sys
import time
import argparse

# Windows console (cp1252) cannot handle umlauts/special characters from the catalog
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

_HERE       = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

from _qt_stub import install_qt_stub, register_plugin_package  # noqa: E402

install_qt_stub()
PKG = register_plugin_package(_PLUGIN_DIR)

import importlib  # noqa: E402
lc  = importlib.import_module(f'{PKG}.local_geocoder')
sd  = importlib.import_module(f'{PKG}.source_downloader')
suw = importlib.import_module(f'{PKG}.source_update_worker')
gw  = importlib.import_module(f'{PKG}.geocoder_worker')


def _print_catalog(cat):
    print(f'{"Bundesland":24} {"Typ":11} {"Format":6} Quelle')
    for bl, e in cat['sources'].items():
        status = f' [{e["status"]}]' if e.get('status') else ''
        print(f'{bl:24} {e["typ"]:11} {e["format"]:6} {e.get("quelle_name", "")}{status}')
        if e['typ'] == sd.MANUAL_TYPE:
            print(f'{"":24} -> manuell: {e.get("grund", "")}')


def main():
    ap = argparse.ArgumentParser(description='Geocoder-DB aus Open-Data-Quellen aufbauen')
    ap.add_argument('db', nargs='?', help='Pfad zur geocoder.db (wird angelegt, falls fehlend)')
    ap.add_argument('--bundesland', nargs='*', help='nur diese Bundesländer (Standard: alle automatischen)')
    ap.add_argument('--force', action='store_true', help='auch bei unverändertem Quellenstand neu laden')
    ap.add_argument('--no-replace', action='store_true', help='Bestand des Bundeslands nicht ersetzen, sondern ergänzen')
    ap.add_argument('--list', action='store_true', help='Quellenkatalog anzeigen und beenden')
    ap.add_argument('--details', action='store_true',
                    help='technische Details (URLs, Dateien, Seiten) mit ausgeben')
    ap.add_argument('--eigene-quellen', metavar='JSON',
                    help='zusätzliche eigene Quellen (sources_user.json aus dem QGIS-Profil)')
    args = ap.parse_args()

    if args.eigene_quellen:
        sd.set_user_catalog_path(os.path.abspath(args.eigene_quellen))
    cat = sd.load_catalog()
    for err in cat.get('_fehler', []):
        print('WARNUNG:', err)
    if args.list or not args.db:
        _print_catalog(cat)
        if not args.db:
            print('\nKein DB-Pfad angegeben — nur Katalog gelistet.')
        return 0

    lc.set_db_path(os.path.abspath(args.db))
    targets = args.bundesland or sd.automatic_bundeslaender(cat)
    skipped = [bl for bl in targets if not sd.is_automatic(cat['sources'].get(bl))]
    targets = [bl for bl in targets if bl not in skipped]
    for bl in skipped:
        e = cat['sources'].get(bl) or {}
        print(f'ÜBERSPRUNGEN {bl}: {e.get("grund", "keine Quelle im Katalog")}')

    if not targets:
        print('Keine automatische Quelle ausgewählt.')
        return 0
    # One worker for all states: downloads only when there is new data, imports each
    # state in one transaction and builds the directory ONCE at the end.
    t0 = time.time()
    w = suw.SourceUpdateWorker(targets, force=args.force, replace=not args.no_replace,
                               plugin_version=gw.PLUGIN_VERSION)
    state = {'last': 0.0}

    def _progress(done, total, text):
        now = time.time()
        if now - state['last'] >= 2.0 or (total and done >= total):
            state['last'] = now
            print(f'  … {text}')

    result = {}
    w.log.connect(lambda m: print('  ' + m))
    if args.details:
        w.detail.connect(lambda m: print('    [Detail] ' + m))
    w.progress.connect(_progress)
    w.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    w.run()
    print(f'\n{result.get("msg")}\n({time.time() - t0:,.0f} s)')

    print('\nStatistik:')
    for bl, n in lc.get_db_stats().items():
        print(f'  {bl:24} {n:>12,}')
    failed = [bl for bl, st, _t in w.summary if st in ('fehler', 'abgebrochen')]
    if failed:
        print('\nFehlgeschlagen:', ', '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
