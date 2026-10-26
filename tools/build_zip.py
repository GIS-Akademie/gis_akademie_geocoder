# -*- coding: utf-8 -*-
"""
Builds the plugin package dist/gis_akademie_geocoder_<version>.zip and checks it
against the rules of the official QGIS plugin repository (plugins.qgis.org,
upload validator and security scan, as of October 2026):

  * one folder gis_akademie_geocoder/ with __init__.py, metadata.txt and LICENSE
  * required entries in metadata.txt: name, description, version, qgisMinimumVersion,
    author, email, about, tracker, repository (+ homepage, no placeholders)
  * no .pyc files, no __pycache__, .git, __MACOSX folders
  * no executable/binary extensions (.exe .dll .so .dylib .bat .sh .ps1 .cmd)
  * no hidden files, no backslashes or drive letters in names
  * size at most 25 MB

The package contains the files tracked by git — without tests, tools and
repository helper files. That way the content matches the repository (a rule of
the plugin repository).

Usage:
    python tools/build_zip.py                 # build and check
    python tools/build_zip.py --urls          # additionally fetch repository/tracker/homepage
"""
import os
import re
import sys
import zipfile
import argparse
import subprocess  # nosec B404 - only git ls-files, fixed arguments
import configparser
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE    = 'gis_akademie_geocoder'
MAX_BYTES  = 25_000_000
REQUIRED   = ('name', 'description', 'version', 'qgisMinimumVersion', 'author', 'email',
              'about', 'tracker', 'repository', 'homepage')
PLACEHOLDER_URLS = ('http://bugs', 'http://repo', 'http://homepage')
EXCLUDE_PREFIXES = ('tests/', 'tools/', 'dist/', 'material/', '.github/')
EXCLUDE_FILES    = {'.gitignore', '.gitattributes'}
SUSPICIOUS       = ('.exe', '.dll', '.so', '.dylib', '.bat', '.sh', '.ps1', '.cmd')
FORBIDDEN_DIRS   = ('__MACOSX', '.git', '__pycache__')


def tracked_files():
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=PLUGIN_DIR, check=True,  # nosec B603 B607
                         stdout=subprocess.PIPE).stdout.decode('utf-8')
    files = [f for f in out.split('\0') if f]
    return sorted(f for f in files
                  if not f.startswith(EXCLUDE_PREFIXES) and f not in EXCLUDE_FILES)


def read_metadata():
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(os.path.join(PLUGIN_DIR, 'metadata.txt'), encoding='utf-8')
    return dict(cp['general'])


def check_metadata(meta, errors, warnings):
    for key in REQUIRED:
        if not meta.get(key, '').strip():
            errors.append(f'metadata.txt: Pflichtangabe „{key}“ fehlt')
    for key in ('tracker', 'repository', 'homepage'):
        url = meta.get(key, '')
        if url in PLACEHOLDER_URLS or not url.startswith(('https://', 'http://')):
            errors.append(f'metadata.txt: „{key}“ ist keine gültige Adresse ({url!r})')
    if '/' in meta.get('author', ''):
        errors.append('metadata.txt: author darf keinen Schrägstrich enthalten')
    if 'supportsQt6' in meta:
        warnings.append('metadata.txt: supportsQt6 gilt im Verzeichnis als veraltet')
    if not re.match(r'^\d+\.\d+(\.\d+)?$', meta.get('version', '')):
        errors.append(f'metadata.txt: Version {meta.get("version")!r} ist keine Versionsnummer')
    try:
        hilfe = open(os.path.join(PLUGIN_DIR, 'help.html'), encoding='utf-8').read()
        m = re.search(r'Dokumentation v([0-9.]+)', hilfe)
        if m and m.group(1) != meta.get('version'):
            errors.append(f'help.html nennt Version {m.group(1)}, metadata.txt {meta.get("version")}')
    except OSError:
        pass
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import build_help_tables
        cat = __import__('json').load(open(os.path.join(PLUGIN_DIR, 'sources.json'), encoding='utf-8'))
        if build_help_tables.render(hilfe, cat) != hilfe:
            errors.append('help.html passt nicht zu sources.json — tools/build_help_tables.py ausführen')
    except (OSError, NameError, SystemExit) as e:
        warnings.append(f'Quellentabellen der Hilfe nicht geprüft ({e})')
    icon = meta.get('icon', '').lstrip('./')
    if icon and not os.path.exists(os.path.join(PLUGIN_DIR, icon)):
        errors.append(f'metadata.txt: Symbol {icon} fehlt')


def check_names(names, errors, warnings):
    for n in names:
        parts = n.split('/')
        if '..' in n or n.startswith('/') or '\\' in n or re.match(r'^[A-Za-z]:', n):
            errors.append(f'ungültiger Pfad im Zip: {n}')
        if n.endswith('.pyc'):
            errors.append(f'.pyc-Datei im Zip: {n}')
        for d in FORBIDDEN_DIRS:
            if d in parts:
                errors.append(f'verbotener Ordner {d} im Zip: {n}')
        if n.lower().endswith(SUSPICIOUS):
            errors.append(f'ausführbare/binäre Datei im Zip: {n}')
        if parts[-1].startswith('.'):
            warnings.append(f'versteckte Datei im Zip: {n}')
    for need in ('__init__.py', 'metadata.txt', 'LICENSE'):
        if f'{PACKAGE}/{need}' not in names:
            errors.append(f'{need} fehlt im Plugin-Ordner des Zips')
    if not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', PACKAGE):
        errors.append('Ordnername ist kein gültiger Python-Bezeichner')


def check_urls(meta, errors):
    for key in ('repository', 'tracker', 'homepage'):
        url = meta.get(key, '')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'build_zip'}, method='GET')
            with urllib.request.urlopen(req, timeout=20) as r:  # nosec B310 - own https addresses
                ok = 200 <= r.status < 400
        except Exception as e:
            ok, why = False, str(e)
        else:
            why = ''
        if not ok:
            errors.append(f'{key} nicht erreichbar ({url}) {why} — beim Upload prüft das '
                          'Verzeichnis die Erreichbarkeit; das Repository muss öffentlich sein.')


def main():
    ap = argparse.ArgumentParser(description='Plugin-Zip bauen und prüfen')
    ap.add_argument('--urls', action='store_true', help='repository/tracker/homepage abrufen')
    args = ap.parse_args()

    meta = read_metadata()
    errors, warnings = [], []
    check_metadata(meta, errors, warnings)
    files = tracked_files()
    untracked_needed = [f for f in ('LICENSE',) if f not in files]
    for f in untracked_needed:
        errors.append(f'{f} ist nicht in git eingecheckt')
    names = [f'{PACKAGE}/{f}' for f in files]
    check_names(names, errors, warnings)
    if args.urls:
        check_urls(meta, errors)

    out_dir = os.path.join(PLUGIN_DIR, 'dist')
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f'{PACKAGE}_{meta.get("version", "0")}.zip')
    if not errors:
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in files:
                z.write(os.path.join(PLUGIN_DIR, f), f'{PACKAGE}/{f}')
        size = os.path.getsize(out)
        with zipfile.ZipFile(out) as z:
            if z.testzip() is not None:
                errors.append('Zip fehlerhaft')
        if size > MAX_BYTES:
            errors.append(f'Zip ist {size / 1e6:.1f} MB groß, erlaubt sind höchstens 25 MB')
    for w in warnings:
        print('Hinweis:', w)
    if errors:
        for e in errors:
            print('FEHLER:', e)
        if os.path.exists(out):
            os.remove(out)
        return 1
    print(f'{out}: {len(files)} Dateien, {size / 1e6:.1f} MB — alle Prüfungen bestanden.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
