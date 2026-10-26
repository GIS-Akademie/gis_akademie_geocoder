# -*- coding: utf-8 -*-
"""
Automatic download of the geocoding reference data (open-data house
coordinates) per Bundesland based on the source catalog sources.json.

QGIS-free (standard library only), so the module can also be used by the build
script tools/build_db.py and by the test suite.

Flow (update_source):
  1. Read the catalog entry; 'manuell' → return immediately (hint for the user).
  2. Determine the file URL(s) (direct, directory index, page/feed via regex,
     CKAN API) or fetch WFS page by page.
  3. Download into the cache folder <DB folder>/quellen/<Bundesland>/ —
     with If-None-Match / If-Modified-Since against the last state from the
     source_meta table; unchanged → status 'unchanged', no import needed.
  4. Extract the zip, select data files using the 'entpacken' regex.
  Returns: dict(status, files, format, meta). The import itself happens in the
  caller (source_update_worker.SourceUpdateWorker), which uses the existing
  import workers.

Usage rules: only openly licensed sources with direct access; a descriptive
User-Agent with contact address; no shops, registrations or portals with bot
protection (these are listed in the catalog as 'manuell').
"""

import os
import re
import json
import shutil
import hashlib
import zipfile
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, formatdate

PLUGIN_DIR   = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(PLUGIN_DIR, 'sources.json')

# The user's own sources (survive plugin updates): set by the plugin at
# startup to <QGIS profile>/gis_akademie_geocoder/sources_user.json.
USER_CATALOG_PATH = None

# Online source list: the same sources.json from the plugin's repository,
# cached in <QGIS profile>/gis_akademie_geocoder/sources_online.json.
# This allows shipping changed links without reinstalling the plugin.
# It is only fetched on click ("Quellenliste prüfen" or before
# "Alle eingetragenen Quellen aktualisieren"), never at QGIS startup.
ONLINE_CATALOG_PATH = None
DEFAULT_CATALOG_URL = ('https://raw.githubusercontent.com/GIS-Akademie/gis_akademie_geocoder/'
                       'main/sources.json')
CATALOG_SCHEMA = 1                 # highest catalog version this plugin understands
_MAX_CATALOG_BYTES = 2_000_000

BUNDESLAENDER = (
    'Baden-Württemberg', 'Bayern', 'Berlin', 'Brandenburg', 'Bremen', 'Hamburg',
    'Hessen', 'Mecklenburg-Vorpommern', 'Niedersachsen', 'Nordrhein-Westfalen',
    'Rheinland-Pfalz', 'Saarland', 'Sachsen', 'Sachsen-Anhalt',
    'Schleswig-Holstein', 'Thüringen',
)

TYPE_LABELS = {
    'datei':      'Datei auf diesem Rechner',
    'direct':     'Feste Datei-URL',
    'index_json': 'Verzeichnisindex (index.json)',
    'page_link':  'Link auf einer Webseite (Regex)',
    'atom':       'Atom-Feed (Regex)',
    'rss':        'RSS-Feed (Regex)',
    'ckan':       'CKAN-Datenkatalog (API)',
    'wfs':        'WFS 2.0 (seitenweise GML)',
    'index_geojson': 'Dateiliste als GeoJSON (je Kreis eine Datei)',
    'gaialight':  'GaiaLight-Downloaddienst (Auftrag)',
    'ogcapi':     'OGC API Features (GeoJSON-Seiten)',
    'manuell':    'manuell',
}

AUTOMATIC_TYPES = ('direct', 'index_json', 'page_link', 'atom', 'rss', 'ckan', 'wfs',
                   'index_geojson', 'gaialight', 'ogcapi')
FILE_TYPE       = 'datei'      # registered file on this computer (path instead of URL)
MANUAL_TYPE     = 'manuell'
FORMATS         = ('hkde', 'gml', 'shp', 'osm', 'alkis', 'geojson')
# osm: OpenStreetMap extract (.osm.pbf); alkis: ALKIS GeoPackage with buildings and land parcels

CHUNK = 1024 * 256


class Cancelled(Exception):
    """Cancelled by the user."""


class SourceError(Exception):
    """Source not retrievable / catalog invalid. str(e) is worded for end
    users; e.detail contains technical details (URL, pattern …) for the
    QGIS message log."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


def _n(x):
    """Integer with German thousands separator (dot)."""
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


def _mb(nbytes):
    """Bytes as 'x,y' MB (German notation)."""
    return f'{nbytes / 1e6:,.1f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


_SOURCE_CHANGED = ('Die Download-Datei wurde bei der Quelle nicht gefunden – die Quelle hat sich '
                   'vermutlich geändert. Bitte im Reiter „Datenbank & Quellen“ prüfen oder der '
                   'GIS-Akademie melden.')


def _host(url):
    return urllib.parse.urlparse(url).netloc or url


def _http_error(code, url):
    host = _host(url)
    if code == 404:
        msg = (f'Die Datei ist auf dem Server {host} nicht (mehr) vorhanden – die Quelle hat '
               'sich vermutlich geändert. Bitte im Reiter „Datenbank & Quellen“ prüfen oder der '
               'GIS-Akademie melden.')
    elif code in (401, 403):
        msg = f'Der Server {host} verweigert den Zugriff (Fehler {code}).'
    elif code == 429:
        msg = f'Der Server {host} meldet zu viele Anfragen. Bitte später erneut versuchen.'
    elif code >= 500:
        msg = f'Der Server {host} ist derzeit gestört (Fehler {code}). Bitte später erneut versuchen.'
    else:
        msg = f'Der Server {host} meldet Fehler {code}.'
    return SourceError(msg, detail=f'HTTP {code} bei {url}')


def _net_error(url, reason):
    return SourceError(f'Keine Verbindung zum Server {_host(url)}. Bitte die Internetverbindung '
                       'prüfen und später erneut versuchen.',
                       detail=f'Netzwerkfehler bei {url}: {reason}')


# ── Catalog ──────────────────────────────────────────────────────────────────

def set_user_catalog_path(path):
    global USER_CATALOG_PATH
    USER_CATALOG_PATH = path


def set_online_catalog_path(path):
    global ONLINE_CATALOG_PATH
    ONLINE_CATALOG_PATH = path


def load_user_catalog(path=None):
    """User's own sources {bundesland: entry} (empty if there is no file)."""
    path = path or USER_CATALOG_PATH
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return dict(data.get('sources', {}))


def _write_user_catalog(sources, path=None):
    path = path or USER_CATALOG_PATH
    if not path:
        raise SourceError('Kein Speicherort für eigene Quellen gesetzt.')
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'version': 1, 'sources': sources}, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def save_user_source(bundesland, entry, path=None):
    """Save a user source for a Bundesland (replaces the default entry).
    It is validated first; internal fields (_…) are removed."""
    clean = {k: v for k, v in entry.items() if not k.startswith('_') and v not in (None, '', [])}
    validate_catalog({'sources': {bundesland: clean}})
    sources = load_user_catalog(path)
    sources[bundesland] = clean
    _write_user_catalog(sources, path)
    return clean


def remove_user_source(bundesland, path=None):
    sources = load_user_catalog(path)
    if bundesland in sources:
        del sources[bundesland]
        _write_user_catalog(sources, path)
        return True
    return False


def _iso_date(value):
    return value if isinstance(value, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', value) else ''


def _valid_online_entry(bl, entry):
    """Check an entry of an online list: valid as in the bundled catalog,
    but only internet sources or "manuell" and only http(s) URLs — a list
    from the internet must never point to files on this computer."""
    if not isinstance(entry, dict) or entry.get('typ') == FILE_TYPE:
        raise SourceError(f'{bl}: Typ in der Online-Liste nicht erlaubt')
    url = str(entry.get('url', ''))
    if not url.lower().startswith(('https://', 'http://')):
        raise SourceError(f'{bl}: keine http(s)-Adresse')
    validate_catalog({'sources': {bl: entry}})


def load_online_catalog(path=None):
    """Cached online list or None."""
    path = path or ONLINE_CATALOG_PATH
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) and isinstance(data.get('sources'), dict) else None


def _merge_online(cat, online):
    """Overlay the online list on the bundled catalog — only if it is newer
    and this plugin understands its format. Individual invalid or unknown
    entries (e.g. a new fetch type for a later version) are skipped; the
    bundled entry then applies for that state."""
    stand_on, stand_mit = _iso_date(online.get('stand')), _iso_date(cat.get('stand'))
    if not stand_on or stand_on <= stand_mit:
        return
    if int(online.get('version', 1) or 1) > CATALOG_SCHEMA:
        cat['_fehler'].append('Die online verfügbare Quellenliste braucht eine neuere Version des '
                              'Plugins — es gilt die mitgelieferte Liste.')
        return
    ersetzt = []
    for bl, entry in online['sources'].items():
        try:
            _valid_online_entry(bl, entry)
        except Exception:
            continue
        entry = {k: v for k, v in entry.items() if not str(k).startswith('_')}
        entry['_herkunft'] = 'standard'
        cat['sources'][bl] = entry
        ersetzt.append(bl)
    if ersetzt:
        cat['stand'] = stand_on
        cat['_quelle'] = 'online'
        meta = online.get('_online') or {}
        cat['_online_abgerufen'] = meta.get('abgerufen', '')
        if online.get('katalog_url'):
            cat['katalog_url'] = online['katalog_url']


def load_catalog(path=None, include_user=True, include_online=True):
    """Default catalog (sources.json), possibly overlaid by a newer online
    list, plus the user's own sources. Each entry gets '_herkunft' =
    'standard' | 'eigene'. Invalid user entries are skipped and noted in
    cat['_fehler'] (the Bundesland's default source then stays active).
    cat['_quelle'] = 'mitgeliefert' | 'online', cat['stand']."""
    path = path or CATALOG_PATH
    with open(path, encoding='utf-8') as f:
        cat = json.load(f)
    validate_catalog(cat)
    for e in cat['sources'].values():
        e['_herkunft'] = 'standard'
    cat['_fehler'] = []
    cat['_quelle'] = 'mitgeliefert'
    if include_online and path == CATALOG_PATH:
        online = load_online_catalog()
        if online:
            _merge_online(cat, online)
    if include_user and path == CATALOG_PATH:
        try:
            user = load_user_catalog()
        except Exception as e:
            user = {}
            cat['_fehler'].append(f'Eigene Quellen nicht lesbar: {e}')
        for bl, entry in user.items():
            try:
                validate_catalog({'sources': {bl: entry}})
            except Exception as e:
                cat['_fehler'].append(f'{bl}: eigene Quelle ungültig ({e})')
                continue
            entry = dict(entry)
            entry['_herkunft'] = 'eigene'
            cat['sources'][bl] = entry
    return cat


def validate_catalog(cat):
    """Raises SourceError for missing required fields — so a typo in the
    catalog is noticed immediately (also in the test suite)."""
    if not isinstance(cat.get('sources'), dict) or not cat['sources']:
        raise SourceError('sources.json: "sources" fehlt oder ist leer')
    for bl, e in cat['sources'].items():
        typ = e.get('typ')
        if typ not in AUTOMATIC_TYPES + (FILE_TYPE, MANUAL_TYPE):
            raise SourceError(f'{bl}: unbekannter typ {typ!r}')
        if e.get('format') not in FORMATS:
            raise SourceError(f'{bl}: unbekanntes format {e.get("format")!r}')
        if typ == FILE_TYPE:
            if not e.get('pfad'):
                raise SourceError(f'{bl}: Bitte eine Datei auswählen.')
        elif not e.get('url'):
            raise SourceError(f'{bl}: Bitte eine URL angeben.')
        if e.get('format') == 'shp' and typ != MANUAL_TYPE:
            felder = e.get('felder') or {}
            if not (felder.get('strasse') and felder.get('hausnummer')):
                raise SourceError(f'{bl}: Bitte im Shapefile mindestens die Felder für Straße '
                                  'und Hausnummer zuordnen.')
        if typ == 'index_json' and not e.get('datei_muster'):
            raise SourceError(f'{bl}: Bitte ein Dateiname-Muster angeben.')
        if typ in ('page_link', 'atom', 'rss') and not e.get('link_muster'):
            raise SourceError(f'{bl}: Bitte ein Link-Muster angeben.')
        if typ == 'ckan' and not (e.get('abfrage') and e.get('ressource_muster')):
            raise SourceError(f'{bl}: Bitte CKAN-Abfrage und Ressourcen-Muster angeben.')
        if typ == 'ogcapi':
            felder = e.get('felder') or {}
            if not (felder.get('strasse') and felder.get('hausnummer')):
                raise SourceError(f'{bl}: Bitte die Felder für Straße und Hausnummer angeben.')
        if e.get('ausweich'):
            if not isinstance(e['ausweich'], dict) or e['ausweich'].get('typ') not in AUTOMATIC_TYPES:
                raise SourceError(f'{bl}: Die Ausweichquelle muss eine Internetquelle sein.')
            validate_catalog({'sources': {f'{bl} (Ausweichquelle)': e['ausweich']}})
        if typ == 'gaialight' and not e.get('objekt'):
            raise SourceError(f'{bl}: Bitte das Objekt des Downloaddienstes angeben (z.B. hk_alkis).')
        if typ == 'wfs' and not e.get('typnamen'):
            raise SourceError(f'{bl}: Bitte die WFS-Typnamen angeben.')
        if typ == 'wfs' and e.get('abruf', 'seiten') not in ('seiten', 'kacheln'):
            raise SourceError(f'{bl}: abruf muss "seiten" oder "kacheln" sein')
        if typ == 'wfs' and e.get('abruf') == 'kacheln':
            bb = e.get('kachel_bbox')
            if not e.get('kachel_crs') or not (isinstance(bb, list) and len(bb) == 4):
                raise SourceError(f'{bl}: kacheln braucht kachel_crs und kachel_bbox [x0,y0,x1,y1]')
        if typ == MANUAL_TYPE and not e.get('grund'):
            raise SourceError(f'{bl}: grund fehlt (warum manuell?)')
        for key in ('datei_muster', 'link_muster', 'ressource_muster', 'entpacken'):
            if e.get(key):
                try:
                    re.compile(e[key])
                except re.error as err:
                    raise SourceError(f'{bl}: Das Muster „{e[key]}“ ist kein gültiger '
                                      f'regulärer Ausdruck ({err}).')


def source_for(bundesland, catalog=None):
    cat = catalog or load_catalog()
    return cat['sources'].get(bundesland)


def is_automatic(entry):
    """Internet source that is fetched without user intervention."""
    return bool(entry) and entry.get('typ') in AUTOMATIC_TYPES


def is_file_source(entry):
    return bool(entry) and entry.get('typ') == FILE_TYPE


def is_registered(entry):
    """Registered source (internet or file) — included by "Alle eingetragenen
    Quellen aktualisieren" (update all registered sources)."""
    return is_automatic(entry) or is_file_source(entry)


def automatic_bundeslaender(catalog=None):
    cat = catalog or load_catalog()
    return [bl for bl, e in cat['sources'].items() if is_automatic(e)]


def registered_bundeslaender(catalog=None):
    """Bundesländer with a registered source (internet or file), in table
    order."""
    cat = catalog or load_catalog()
    order = list(BUNDESLAENDER) + [bl for bl in cat['sources'] if bl not in BUNDESLAENDER]
    return [bl for bl in order if is_registered(cat['sources'].get(bl))]


def describe(entry):
    """Short text for the user interface."""
    if not entry:
        return 'Für dieses Bundesland ist keine Quelle hinterlegt.'
    name = entry.get('quelle_name', entry.get('url', ''))
    if entry.get('typ') == MANUAL_TYPE:
        return (f'{name} — manueller Download: {entry.get("grund", "")} '
                f'Lizenz: {entry.get("lizenz", "?")}.')
    if entry.get('typ') == FILE_TYPE:
        parts = [f'Datei {entry.get("pfad", "")}']
        if entry.get('quelle_name'):
            parts.append(entry['quelle_name'])
        if entry.get('lizenz'):
            parts.append(f'Lizenz: {entry["lizenz"]}')
        return ' · '.join(parts)
    status = ' (experimentell)' if entry.get('status') == 'experimentell' else ''
    parts = [f'{name}{status}', f'Lizenz: {entry.get("lizenz", "?")}']
    if entry.get('turnus'):
        parts.append(f'Aktualisierung: {entry["turnus"]}')
    if entry.get('hinweis'):
        parts.append(entry['hinweis'])
    return ' · '.join(parts)


def cache_dir_for(db_path, bundesland):
    """<DB folder>/quellen/<Bundesland without special characters>/"""
    base = os.path.dirname(os.path.abspath(db_path)) or '.'
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', bundesland).strip('_')
    return os.path.join(base, 'quellen', safe)


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Http:
    """Thin wrapper around urllib — replaceable in tests (fake with the same
    methods). get() returns (status, headers_dict, body_bytes); 304 is
    returned as a status, other errors are raised as SourceError."""

    def __init__(self, user_agent, timeout=60):
        self.user_agent = user_agent
        self.timeout    = timeout

    def _request(self, url, headers=None):
        h = {'User-Agent': self.user_agent, 'Accept': '*/*'}
        h.update(headers or {})
        return urllib.request.Request(url, headers=h)

    def get(self, url, headers=None, max_bytes=None):
        try:
            with urllib.request.urlopen(self._request(url, headers), timeout=self.timeout) as r:
                body = r.read(max_bytes) if max_bytes else r.read()
                return r.status, {k.lower(): v for k, v in r.headers.items()}, body
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, {k.lower(): v for k, v in e.headers.items()}, b''
            raise _http_error(e.code, url) from e
        except urllib.error.URLError as e:
            raise _net_error(url, e.reason) from e

    def download(self, url, dest, headers=None, progress=None, cancel=None):
        """Streams url to dest (via a temporary file). Returns
        (status, headers_dict). On 304 nothing is written."""
        tmp = dest + '.part'
        try:
            with urllib.request.urlopen(self._request(url, headers), timeout=self.timeout) as r:
                hdrs  = {k.lower(): v for k, v in r.headers.items()}
                total = int(hdrs.get('content-length') or 0)
                done  = 0
                sha   = hashlib.sha256()
                with open(tmp, 'wb') as f:
                    while True:
                        if cancel and cancel():
                            raise Cancelled()
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                hdrs['_sha256'] = sha.hexdigest()
                hdrs['_size']   = str(done)
            os.replace(tmp, dest)
            return r.status, hdrs
        except urllib.error.HTTPError as e:
            _unlink(tmp)
            if e.code == 304:
                return 304, {k.lower(): v for k, v in e.headers.items()}
            raise _http_error(e.code, url) from e
        except urllib.error.URLError as e:
            _unlink(tmp)
            raise _net_error(url, e.reason) from e
        except BaseException:
            _unlink(tmp)
            raise


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ── URL resolution ───────────────────────────────────────────────────────────

def _absolute(base, link):
    return urllib.parse.urljoin(base, link.strip())


_TEMPLATE_KEYS = ('{datum}', '{jahr}', '{nr}')


def url_template_candidates(url, today=None):
    """Candidates for a URL with placeholders, newest first:
      {datum} — today's date YYYYMMDD (then yesterday, in case the server does
                not have today's folder yet),
      {jahr}/{nr} — data release, e.g. "-2026-01": current and previous
                year, number 12 down to 1 (two digits).
    Example Hessen: …/downloadcenter/{datum}/…/Hauskoordinaten…-{jahr}-{nr}.zip"""
    from datetime import date, timedelta
    today = today or date.today()
    tage = [today, today - timedelta(days=1)] if '{datum}' in url else [today]
    if '{jahr}' in url or '{nr}' in url:
        fassungen = [(j, n) for j in (today.year, today.year - 1)
                     for n in range(12, 0, -1)] if '{nr}' in url else                     [(j, 1) for j in (today.year, today.year - 1)]
    else:
        fassungen = [(today.year, 1)]
    out = []
    for tag in tage:
        for j, n in fassungen:
            u = (url.replace('{datum}', tag.strftime('%Y%m%d'))
                    .replace('{jahr}', str(j)).replace('{nr}', f'{n:02d}'))
            if u not in out:
                out.append(u)
    return out


def resolve_url_template(url, http, today=None):
    """First reachable URL from url_template_candidates — checked by fetching
    the first bytes (some servers do not allow HEAD requests)."""
    tried = []
    for cand in url_template_candidates(url, today):
        try:
            status, _h, _b = http.get(cand, headers={'Range': 'bytes=0-3'}, max_bytes=4)
        except SourceError:
            tried.append(cand)
            continue
        if status in (200, 206):
            return cand
        tried.append(cand)
    raise SourceError(_SOURCE_CHANGED,
                      detail=f'Keine der {len(tried)} Adressen erreichbar, Muster {url}')


def index_geojson_urls(entry, http):
    """File URLs from an index in GeoJSON format (e.g. LGLN Niedersachsen:
    one feature per district/Kreis, field 'zip'). Umlauts in the URLs are encoded."""
    feld = entry.get('url_feld') or 'zip'
    _s, _h, body = http.get(entry['url'])
    try:
        feats = json.loads(body.decode('utf-8')).get('features') or []
    except ValueError as e:
        raise SourceError('Die Dateiliste der Quelle ist nicht lesbar.',
                          detail=f'Index nicht lesbar: {entry["url"]}') from e
    urls = []
    for f in feats:
        u = ((f or {}).get('properties') or {}).get(feld)
        if isinstance(u, str) and u.lower().startswith(('https://', 'http://')):
            u = urllib.parse.quote(u, safe=':/?&=%#+~,;@!$\'()*')
            if u not in urls:
                urls.append(u)
    if not urls:
        raise SourceError(_SOURCE_CHANGED, detail=f'Keine Einträge im Feld {feld!r} in {entry["url"]}')
    return sorted(urls)


def resolve_download_urls(entry, http):
    """Returns the list of file URLs to download for an entry
    (not for wfs/manuell)."""
    typ = entry['typ']
    url = entry['url']
    if typ == 'direct':
        if '{' in url:
            return [resolve_url_template(url, http)]
        return [url]

    if typ == 'index_json':
        status, _, body = http.get(url)
        try:
            idx = json.loads(body.decode('utf-8'))
        except ValueError as e:
            raise SourceError('Die Dateiliste der Quelle ist nicht lesbar.',
                              detail=f'index.json nicht lesbar: {url}') from e
        pat   = re.compile(entry['datei_muster'])
        names = []
        for ds in idx.get('datasets', []):
            for f in ds.get('files', []):
                if pat.search(f.get('name', '')):
                    names.append(f['name'])
        if not names:
            raise SourceError(_SOURCE_CHANGED, detail=f'Keine Datei passend zu '
                              f'{entry["datei_muster"]!r} im Index {url}')
        return [_absolute(url, n) for n in sorted(set(names))]

    if typ in ('page_link', 'atom', 'rss'):
        status, _, body = http.get(url)
        text  = body.decode('utf-8', 'replace')
        links = re.findall(entry['link_muster'], text)
        links = [l if isinstance(l, str) else l[0] for l in links]
        if not links:
            raise SourceError(_SOURCE_CHANGED, detail=f'Kein Link passend zu '
                              f'{entry["link_muster"]!r} auf {url}')
        seen, out = set(), []
        for l in links:
            a = _absolute(url, l)
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    if typ == 'ckan':
        q = urllib.parse.urlencode({'q': entry['abfrage'], 'rows': entry.get('rows', 10),
                                    'sort': 'metadata_modified desc'})
        status, _, body = http.get(f'{url}?{q}')
        try:
            data = json.loads(body.decode('utf-8'))
        except ValueError as e:
            raise SourceError('Die Antwort des Datenportals ist nicht lesbar.',
                              detail=f'CKAN-Antwort nicht lesbar: {url}') from e
        pat  = re.compile(entry['ressource_muster'], re.IGNORECASE)
        best = None   # (date, url)
        for pkg in data.get('result', {}).get('results', []):
            for res in pkg.get('resources', []):
                u = res.get('url', '')
                m = pat.search(u)
                if m:
                    key = m.group(1) if m.groups() else (res.get('created') or '')
                    if best is None or key > best[0]:
                        best = (key, u)
        if not best:
            raise SourceError(_SOURCE_CHANGED, detail=f'Keine CKAN-Ressource passend zu '
                              f'{entry["ressource_muster"]!r}')
        return [best[1]]

    raise SourceError(f'Typ {typ!r} hat keine Datei-URLs')


# ── Download & extraction ────────────────────────────────────────────────────

def _file_name_from_url(url):
    name = os.path.basename(urllib.parse.urlparse(url).path) or 'download.bin'
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name)


def _url_key(url):
    """URL without the daily folder (/YYYYMMDD/) — e.g. Hessen: the same file is
    placed under a new date folder every day, the server returns the same ETag."""
    return re.sub(r'/\d{8}/', '/{datum}/', url or '')


def download_file(http, url, dest_dir, prev_meta=None, force=False,
                  progress=None, cancel=None):
    """Downloads url into dest_dir. prev_meta (dict with etag/last_modified of
    the last successful processing of this URL) controls the conditional
    request; force=True forces the download.

    Returns dict(status='ok'|'unchanged', path, etag, last_modified, size,
    sha256, url)."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, _file_name_from_url(url))
    headers = {}
    if prev_meta and not force and _url_key(prev_meta.get('url')) == _url_key(url):
        if prev_meta.get('etag'):
            headers['If-None-Match'] = prev_meta['etag']
        if prev_meta.get('last_modified'):
            headers['If-Modified-Since'] = prev_meta['last_modified']
    status, hdrs = http.download(url, dest, headers=headers, progress=progress, cancel=cancel)
    if status == 304:
        return {'status': 'unchanged', 'path': dest if os.path.exists(dest) else None,
                'url': url, 'etag': prev_meta.get('etag'),
                'last_modified': prev_meta.get('last_modified'), 'size': None, 'sha256': None}
    if 'text/html' in str(hdrs.get('content-type', '')).lower():
        # Maintenance or error page instead of the file (e.g. Thüringen, status 200/206)
        _unlink(dest)
        raise SourceError(f'Der Server {_host(url)} liefert derzeit keine Datei, sondern eine '
                          'Webseite (z.B. Wartung). Bitte später erneut versuchen – der bisherige '
                          'Bestand bleibt erhalten.', detail=f'text/html statt Datei bei {url}')
    info = {'status': 'ok', 'path': dest, 'url': url,
            'etag': hdrs.get('etag'), 'last_modified': hdrs.get('last-modified'),
            'size': int(hdrs.get('_size') or 0), 'sha256': hdrs.get('_sha256')}
    # No ETag/Last-Modified from the server (or a new URL, e.g. a changing
    # access token in Sachsen), but the same content as last time?
    if (prev_meta and not force and prev_meta.get('sha256')
            and prev_meta['sha256'] == info['sha256']):
        info['status'] = 'unchanged'
    return info


def extract_data_files(archive, dest_dir, muster=None, _tiefe=0):
    """Extracts a zip and returns the data files (regex 'muster' applied to the
    file name; without a pattern all files). A zip inside the zip (e.g. Bremen:
    GEBAEUDEADRESSEN_SHP_FHB.zip contains GEBAEUDEADRESSEN_SHP_FHB_2026_08.zip)
    is extracted as well. Non-zip files are returned unchanged."""
    if not zipfile.is_zipfile(archive):
        return [archive]
    pat = re.compile(muster, re.IGNORECASE) if muster else None
    out = []
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            inner_zip = base.lower().endswith('.zip') and _tiefe < 2
            if pat and not pat.search(base) and not inner_zip:
                continue
            # Prevent zip slip: only the base name is used
            target = os.path.join(dest_dir, base)
            with z.open(info) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            if inner_zip and zipfile.is_zipfile(target):
                try:
                    out.extend(extract_data_files(target, dest_dir, muster, _tiefe + 1))
                except SourceError:
                    pass
                _unlink(target)
                continue
            out.append(target)
    if not out:
        raise SourceError('Im Download wurde keine passende Adressdatei gefunden – die Quelle '
                          'hat sich vermutlich geändert.',
                          detail=f'Keine Datei passend zu {muster!r} in {os.path.basename(archive)}')
    return out


# ── WFS ──────────────────────────────────────────────────────────────────────
#
# Two fetch modes (catalog field 'abruf'):
#   'seiten'  (pages, default) — count/startIndex paging. Only reliable if the
#             server sorts stably (e.g. Hamburg).
#   'kacheln' (tiles) — spatial tiles via BBOX, adaptively quartered until each
#             tile has at most 'max_objekte' hits and thus arrives complete in
#             ONE response. For servers with unstable paging (M-V returns
#             different objects for the same page every time — even with sortBy).
#             Feature types without geometry (street names) are, if they do not
#             fit into one response, fetched specifically via RESOURCEID.
# In both cases, before the import it is checked whether the number of UNIQUE
# objects matches the server's count (numberMatched) — otherwise abort, so that
# a complete dataset is never replaced by an incomplete one.

_MEMBER_RE = re.compile(rb'<(?:wfs:)?member[\s>]')
_HITS_RE   = re.compile(rb'numberMatched="(\d+)"')


def _wfs_url(base, **params):
    p = {'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature'}
    p.update({k: str(v) for k, v in params.items() if v is not None})
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}{urllib.parse.urlencode(p, safe=":,")}'


def wfs_page_url(base, typname, count, start_index):
    return _wfs_url(base, typeNames=typname, count=count, startIndex=start_index)


def _local(typname):
    return typname.split(':')[-1]


_TYPE_DE = {'Address': 'Adressen', 'ThoroughfareName': 'Straßennamen',
            'AdminUnitName': 'Verwaltungseinheiten', 'PostalDescriptor': 'Postleitzahlen'}


def _de(typname):
    return _TYPE_DE.get(_local(typname), _local(typname))


def _feature_ids(path, typname):
    """gml:id of all objects of a type in a GML file (without an XML parser —
    fast even for 40 MB pages). 'Address' does not match 'AddressLocator'."""
    pat = re.compile(rb'<(?:\w+:)?' + _local(typname).encode() + rb'\b[^>]*?gml:id="([^"]+)"')
    with open(path, 'rb') as f:
        return pat.findall(f.read())


def wfs_hits(http, base, typname, bbox=None):
    """numberMatched of a type (optionally within a BBOX 'x0,y0,x1,y1,crs')."""
    _s, _h, body = http.get(_wfs_url(base, typeNames=typname, resultType='hits', bbox=bbox),
                            max_bytes=50000)
    m = _HITS_RE.search(body)
    if not m:
        raise SourceError('Der Dienst der Quelle antwortet nicht wie erwartet – der Abruf ist '
                          'derzeit nicht möglich. Bitte später erneut versuchen.',
                          detail=f'WFS meldet keine Trefferzahl für {typname}'
                          + (f' (BBOX {bbox})' if bbox else ''))
    return int(m.group(1))


def _check_complete(typname, found, expected, log):
    if expected is None:
        return
    if found != expected:
        raise SourceError(
            f'Der Download war unvollständig ({_n(found)} von {_n(expected)} {_de(typname)}). '
            'Der bisherige Datenbestand bleibt erhalten – bitte später erneut versuchen.',
            detail=(f'WFS {typname}: {found} eindeutige Objekte statt numberMatched={expected}. '
                    'Liefert der Server beim Blättern instabile Seiten, im Katalog '
                    'abruf = "kacheln" setzen.'))
    if log:
        log(f'{typname}: vollständig ({_n(found)} Objekte)')


def _fetch_paged(entry, typname, dest_dir, http, cancel, log, progress, expected):
    count = int(entry.get('seitengroesse', 10000))
    safe  = typname.replace(':', '_')
    files, ids = [], set()
    start = page = 0
    while True:
        if cancel and cancel():
            raise Cancelled()
        dest = os.path.join(dest_dir, f'{safe}_{page:04d}.gml')
        http.download(wfs_page_url(entry['url'], typname, count, start), dest, cancel=cancel)
        with open(dest, 'rb') as f:
            n = len(_MEMBER_RE.findall(f.read()))
        if n == 0:
            _unlink(dest)
            break
        files.append(dest)
        ids.update(_feature_ids(dest, typname))
        if log:
            log(f'{typname}: Seite {page + 1}, {_n(n)} Objekte')
        if progress:
            progress(start + n, expected or 0, _de(typname))
        page  += 1
        start += n
        # Do NOT stop when n < count: some servers cap 'count' at their
        # own maximum; only an empty page ends the type.
    _check_complete(typname, len(ids), expected, log)
    return files


def _fetch_tiled(entry, typname, dest_dir, http, cancel, log, progress, expected):
    """Adaptive BBOX tiles until each tile has <= max_objekte hits."""
    limit = int(entry.get('max_objekte', 10000))
    crs   = entry['kachel_crs']
    safe  = typname.replace(':', '_')
    queue = [tuple(float(v) for v in entry['kachel_bbox'])]
    files, ids = [], set()
    k = 0
    while queue:
        if cancel and cancel():
            raise Cancelled()
        x0, y0, x1, y1 = queue.pop(0)
        bbox = f'{x0:.3f},{y0:.3f},{x1:.3f},{y1:.3f},{crs}'
        n = wfs_hits(http, entry['url'], typname, bbox)
        if n == 0:
            continue
        if n > limit:
            if (x1 - x0) < 1.0:
                raise SourceError('Der Dienst der Quelle hat unerwartete Daten geliefert.',
                                  detail=f'Kachel {bbox} enthält {n} Objekte auf weniger als '
                                         '1 m — max_objekte zu klein?')
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            queue.extend([(x0, y0, mx, my), (mx, y0, x1, my), (x0, my, mx, y1), (mx, my, x1, y1)])
            continue
        dest = os.path.join(dest_dir, f'{safe}_{k:04d}.gml')
        http.download(_wfs_url(entry['url'], typeNames=typname, count=limit, bbox=bbox),
                      dest, cancel=cancel)
        got = _feature_ids(dest, typname)
        if len(got) < n:
            raise SourceError('Der Dienst der Quelle hat unvollständige Daten geliefert. '
                              'Bitte später erneut versuchen.',
                              detail=f'Kachel {bbox}: {len(got)} statt {n} Objekte geliefert.')
        files.append(dest)
        ids.update(got)
        k += 1
        if log and k % 10 == 0:
            log(f'{typname}: {k} Kacheln, {_n(len(ids))} Objekte')
        if progress:
            progress(len(ids), expected or 0, _de(typname))
    _check_complete(typname, len(ids), expected, log)
    return files


def _referenced_ids(files, typname):
    """IDs of a type referenced by the (address) files ('…#Thoroughfare…'
    or — Brandenburg — '…/Thoroughfare…' without '#')."""
    pat = re.compile(rb'xlink:href="[^"]*[#/](' + _local(typname).encode() + rb'_[^"/#]+)"')
    out = set()
    for f in files:
        with open(f, 'rb') as fh:
            out.update(pat.findall(fh.read()))
    return sorted(i.decode() for i in out)


def _fetch_by_ids(entry, typname, ids, dest_dir, http, cancel, log, progress=None):
    """Fetch objects specifically via RESOURCEID (types without geometry that
    do not fit into one response)."""
    batch = int(entry.get('id_batch', 100))
    safe  = typname.replace(':', '_')
    files, got = [], set()
    for i in range(0, len(ids), batch):
        if cancel and cancel():
            raise Cancelled()
        part = ids[i:i + batch]
        dest = os.path.join(dest_dir, f'{safe}_ids_{i // batch:04d}.gml')
        http.download(_wfs_url(entry['url'], resourceId=','.join(part)), dest, cancel=cancel)
        got.update(x.decode() for x in _feature_ids(dest, typname))
        files.append(dest)
        if progress:
            progress(len(got), len(ids), _de(typname))
        if log and (i // batch) % 50 == 49:
            log(f'{typname}: {_n(len(got))} von {_n(len(ids))} nachgeladen')
    missing = len(set(ids) - got)
    if missing > max(5, len(ids) // 200):          # more than 0.5 % missing
        raise SourceError(f'{_n(missing)} von {_n(len(ids))} {_de(typname)} konnten nicht geladen '
                          'werden. Bitte später erneut versuchen.',
                          detail=f'RESOURCEID-Abruf {typname}: {missing} IDs ohne Antwort')
    if log:
        log(f'{typname}: {_n(len(got))} per ID geladen'
            + (f', {missing} nicht gefunden' if missing else ''))
    return files


def fetch_wfs(entry, dest_dir, http, progress=None, cancel=None, log=None):
    """Fetch all feature types of the entry and save them as GML files.
    Returns the file list in import order (the GML import reads names/
    administrative units from ALL files in pass 1)."""
    os.makedirs(dest_dir, exist_ok=True)
    for old in os.listdir(dest_dir):
        if old.endswith('.gml'):
            _unlink(os.path.join(dest_dir, old))
    mode   = entry.get('abruf', 'seiten')
    tiled  = entry.get('kachel_typ', 'ad:Address')
    limit  = int(entry.get('max_objekte', 10000))
    check  = entry.get('vollstaendigkeit_pruefen', True)
    types  = list(entry['typnamen'])
    if mode == 'kacheln' and tiled in types:      # addresses first: they provide the IDs
        types.remove(tiled)
        types.insert(0, tiled)
    files = []
    addr_files = []
    for typname in types:
        expected = wfs_hits(http, entry['url'], typname) if check or mode == 'kacheln' else None
        if mode == 'kacheln' and typname == tiled:
            f = _fetch_tiled(entry, typname, dest_dir, http, cancel, log, progress, expected)
            addr_files = f
        elif mode == 'kacheln' and expected is not None and expected > limit:
            ids = _referenced_ids(addr_files, typname)
            if log:
                log(f'{typname}: {_n(expected)} Objekte, zu viele für eine Antwort — lade die '
                    f'{_n(len(ids))} referenzierten per ID')
            f = _fetch_by_ids(entry, typname, ids, dest_dir, http, cancel, log, progress)
        else:
            f = _fetch_paged(entry, typname, dest_dir, http, cancel, log, progress,
                             expected if check else None)
        files.extend(f)
    if not files:
        raise SourceError('Der Dienst der Quelle hat keine Adressen geliefert.',
                          detail=f'WFS lieferte keine Objekte: {entry["url"]}')
    return files


# ── Orchestration ────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _tage_text(alter):
    if alter <= 0:
        return 'heute'
    return 'gestern' if alter == 1 else f'vor {alter} Tagen'


def update_source(bundesland, db_path, prev_meta=None, force=False,
                  progress=None, log=None, cancel=None, catalog=None,
                  http=None, user_agent_version='dev', detail=None):
    """Fetch the source for a Bundesland (without import).

    log(text)    — messages for the user (log window)
    detail(text) — technical details (QGIS message log)
    progress(done, total, text) — total 0 = unknown.
    Returns dict:
      status : 'manual' | 'unchanged' | 'skipped' (lock after download) | 'downloaded'
      files  : data files for the import (if downloaded)
      format : 'hkde' | 'gml' | 'shp'
      entry  : catalog entry
      meta   : new source_meta fields (url, etag, last_modified, sha256, dateien)
      text   : message for log/dialog (if manual/unchanged)
    """
    cat   = catalog or load_catalog()
    entry = cat['sources'].get(bundesland)
    if not entry:
        raise SourceError(f'Für {bundesland} ist keine Quelle hinterlegt.')
    if is_file_source(entry):
        return _update_from_file(bundesland, entry, db_path, prev_meta, force, log, detail)
    if not is_automatic(entry):
        return {'status': 'manual', 'files': [], 'format': entry.get('format'),
                'entry': entry, 'meta': None,
                'text': manual_text(bundesland, entry)}

    ua   = cat.get('user_agent', 'QGIS-GISAkademieGeocoder/{version}').replace(
        '{version}', user_agent_version)
    http = http or Http(ua)
    cache = cache_dir_for(db_path, bundesland)
    os.makedirs(cache, exist_ok=True)
    _log    = log or (lambda m: None)
    _detail = detail or (lambda m: None)
    _detail(f'{bundesland}: Quelle {entry.get("url")} (Typ {entry["typ"]}), Cache {cache}')

    # Lock after a download (min_abstand_tage): NOT a schedule — the plugin never
    # downloads on its own. When updating, the source is only skipped as long as
    # the last download is more recent. For sources without ETag/Last-Modified (WFS)
    # and for sources that change daily (OpenStreetMap extracts) — otherwise every
    # click would re-download the complete dataset (hundreds of MB).
    tage = entry.get('min_abstand_tage')
    if tage and prev_meta and not force and prev_meta.get('heruntergeladen_am'):
        try:
            last = datetime.fromisoformat(prev_meta['heruntergeladen_am'])
            alter = (datetime.now(last.tzinfo) - last).days
        except Exception:
            alter = None
        if alter is not None and alter < int(tage):
            return {'status': 'skipped', 'files': [], 'format': entry['format'],
                    'entry': entry, 'meta': None,
                    'text': f'Zuletzt {_tage_text(alter)} geladen. Diese Quelle wird beim '
                            f'Aktualisieren übersprungen, solange der letzte Abruf weniger als '
                            f'{tage} Tage zurückliegt – sofort laden mit „Auch ohne neue Daten '
                            'erneut laden“.'}

    if entry['typ'] == 'index_geojson':
        return _update_index_geojson(bundesland, entry, http, prev_meta, force, _log, _detail)

    if entry['typ'] == 'ogcapi':
        return _update_ogcapi(bundesland, entry, http, cache, prev_meta, force,
                              progress, _log, _detail, cancel)

    if entry['typ'] == 'gaialight':
        return _update_gaialight(bundesland, entry, http, cache, prev_meta, force,
                                 progress, _log, _detail, cancel)

    if entry['typ'] == 'wfs':
        _log(f'Quelle: {entry.get("quelle_name", "")} · Lizenz {entry.get("lizenz", "?")}')
        _log('Lade Adressen vom Dienst der Landesvermessung'
             + (f' – das dauert {entry["dauer"]} …' if entry.get('dauer') else ' …'))

        def _wfs_progress(done, total, label):
            if progress:
                txt = (f'{_n(done)} von {_n(total)} {label} geladen …' if total
                       else f'{_n(done)} {label} geladen …')
                progress(done, total, txt)

        files = fetch_wfs(entry, cache, http, progress=_wfs_progress, cancel=cancel, log=_detail)
        sha = hashlib.sha256()
        size = 0
        for f in files:
            size += os.path.getsize(f)
            with open(f, 'rb') as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b''):
                    sha.update(chunk)
        digest = sha.hexdigest()
        if prev_meta and not force and prev_meta.get('sha256') == digest:
            return {'status': 'unchanged', 'files': [], 'format': entry['format'],
                    'entry': entry, 'meta': None,
                    'text': 'Keine neuen Daten seit dem letzten Import.'}
        _log(f'Download abgeschlossen ({_mb(size)} MB).')
        _detail(f'{len(files)} GML-Dateien, SHA-256 {digest}')
        meta = {'url': entry['url'], 'etag': None, 'last_modified': None, 'sha256': digest,
                'dateien': ';'.join(os.path.basename(f) for f in files),
                'heruntergeladen_am': _now_iso(), 'quelle': entry.get('quelle_name', '')}
        return {'status': 'downloaded', 'files': files, 'format': entry['format'],
                'entry': entry, 'meta': meta, 'text': ''}

    _log(f'Quelle: {entry.get("quelle_name", "")} · Lizenz {entry.get("lizenz", "?")}')
    if entry.get('dauer'):
        _log(f'Download und Import dauern {entry["dauer"]}.')
    urls = resolve_download_urls(entry, http)
    _detail(f'{len(urls)} Datei(en) ermittelt: ' + ', '.join(urls))
    all_files, infos, unchanged = [], [], True
    for i, url in enumerate(urls):
        if cancel and cancel():
            raise Cancelled()
        pm = prev_meta if (prev_meta and len(urls) == 1) else None

        def _p(done, total, _i=i, _k=len(urls)):
            if progress:
                txt = (f'Lade Daten: {_mb(done)} MB' + (f' von {_mb(total)} MB' if total else '')
                       + (f' (Datei {_i + 1} von {_k})' if _k > 1 else '') + ' …')
                progress(done, total, txt)
        info = download_file(http, url, cache, prev_meta=pm, force=force,
                             progress=_p, cancel=cancel)
        infos.append(info)
        if info['status'] != 'unchanged':
            unchanged = False
            stand = format_last_modified(info.get('last_modified'))
            _log(f'Download abgeschlossen ({_mb(info["size"])} MB'
                 + (f', Datenstand {stand}' if stand else '') + ').')
    # Unchanged according to server/checksum: this state is already imported (source_meta
    # is in the same DB) — the file in the cache folder is not needed for that.
    if unchanged:
        return {'status': 'unchanged', 'files': [], 'format': entry['format'],
                'entry': entry, 'meta': None,
                'text': 'Keine neuen Daten seit dem letzten Import.'}

    for info in infos:
        all_files.extend(extract_data_files(info['path'], cache, entry.get('entpacken')))
    _detail('Datendateien: ' + ', '.join(os.path.basename(f) for f in all_files))
    first = infos[0]
    meta  = {'url': first['url'], 'etag': first.get('etag'),
             'last_modified': first.get('last_modified'), 'sha256': first.get('sha256'),
             'dateien': ';'.join(os.path.basename(f) for f in all_files),
             'heruntergeladen_am': _now_iso(), 'quelle': entry.get('quelle_name', '')}
    return {'status': 'downloaded', 'files': all_files, 'format': entry['format'],
            'entry': entry, 'meta': meta, 'text': ''}


def fetch_online_catalog(http=None, user_agent_version='dev', url=None):
    """Fetch the online source list (conditionally via ETag) and cache it.
    Returns (status, text) with status 'neu' | 'aktuell' | 'zu_neu' | 'fehler'.
    Never raises — without network the previous list simply stays in effect."""
    if not ONLINE_CATALOG_PATH:
        return 'fehler', 'Kein Speicherort für die Online-Quellenliste gesetzt.'
    try:
        base = load_catalog(include_user=False, include_online=False)
    except Exception as e:
        return 'fehler', f'Mitgelieferte Quellenliste nicht lesbar: {e}'
    alt = load_online_catalog() or {}
    url = url or alt.get('katalog_url') or base.get('katalog_url') or DEFAULT_CATALOG_URL
    ua = base.get('user_agent', 'QGIS-GISAkademieGeocoder/{version}').replace('{version}', user_agent_version)
    http = http or Http(ua, timeout=30)
    headers = {}
    meta_alt = alt.get('_online') or {}
    if meta_alt.get('etag') and meta_alt.get('url') == url:
        headers['If-None-Match'] = meta_alt['etag']
    try:
        status, hdrs, body = http.get(url, headers=headers, max_bytes=_MAX_CATALOG_BYTES + 1)
    except SourceError:
        stand = max(_iso_date(base.get('stand')), _iso_date(alt.get('stand')))
        return 'fehler', ('Die Quellenliste ist online derzeit nicht erreichbar — es gilt die '
                          f'bisherige (Stand {_datum_de(stand)}).')
    jetzt = _now_iso()
    if status == 304 and alt:
        alt.setdefault('_online', {})['abgerufen'] = jetzt
        _write_json(ONLINE_CATALOG_PATH, alt)
        return 'aktuell', _stand_text(base, alt)
    if len(body) > _MAX_CATALOG_BYTES:
        return 'fehler', 'Die online abgerufene Quellenliste ist zu groß und wird nicht verwendet.'
    try:
        data = json.loads(body.decode('utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('sources'), dict):
            raise ValueError('keine Quellenliste')
    except Exception:
        return 'fehler', 'Die online abgerufene Quellenliste ist ungültig und wird nicht verwendet.'
    if int(data.get('version', 1) or 1) > CATALOG_SCHEMA:
        return 'zu_neu', ('Die online verfügbare Quellenliste braucht eine neuere Version des Plugins. '
                          'Bitte das Plugin aktualisieren; bis dahin gilt die bisherige Liste.')
    neu = _iso_date(data.get('stand')) > _iso_date((alt or base).get('stand')) \
        and _iso_date(data.get('stand')) > _iso_date(base.get('stand'))
    data['_online'] = {'url': url, 'etag': hdrs.get('etag', ''), 'abgerufen': jetzt}
    _write_json(ONLINE_CATALOG_PATH, data)
    if neu:
        return 'neu', f'Quellenliste aktualisiert: Stand {_datum_de(data.get("stand"))}.'
    return 'aktuell', _stand_text(base, data)


def _datum_de(iso):
    iso = _iso_date(iso)
    return f'{iso[8:10]}.{iso[5:7]}.{iso[:4]}' if iso else 'unbekannt'


def _stand_text(base, online):
    stand = max(_iso_date(base.get('stand')), _iso_date(online.get('stand')))
    return f'Die Quellenliste ist aktuell (Stand {_datum_de(stand)}).'


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _ogc_get_json(http, url):
    _s, _h, body = http.get(url)
    try:
        return json.loads(body.decode('utf-8'))
    except ValueError as e:
        raise SourceError('Der Dienst hat keine lesbaren Daten geliefert. Bitte später erneut versuchen.',
                          detail=f'kein JSON von {url}') from e


def _ogc_page_url(entry, offset):
    base = entry['url']
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}' + urllib.parse.urlencode(
        {'f': 'json', 'limit': int(entry.get('seitengroesse') or 2500), 'offset': offset})


def _update_ogcapi(bundesland, entry, http, cache, prev_meta, force, progress, _log, _detail,
                   cancel):
    """OGC API Features (e.g. Saarland): load all pages as GeoJSON and write
    them into an HK-BY address file (field mapping 'felder'). Change
    detection via the file's checksum (the service provides no ETag)."""
    from .osm_convert import HK_BY_HEADER, split_housenumbers, _LANDSCHL
    from .plz_resolver import _utm_forward
    felder = entry['felder']
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, 'adressen_ogcapi.txt')
    _log(f'Quelle: {entry.get("quelle_name", "")} · Lizenz {entry.get("lizenz", "?")}')
    _log('Lade Adressen vom Dienst der Landesvermessung'
         + (f' – das dauert {entry["dauer"]} …' if entry.get('dauer') else ' …'))
    landschl = _LANDSCHL.get(bundesland, '')
    sha, n_ok, total, offset, seen = hashlib.sha256(), 0, 0, 0, set()
    limit = int(entry.get('seitengroesse') or 2500)

    def val(props, key):
        k = felder.get(key)
        v = props.get(k) if k else None
        return '' if v is None else str(v).replace(';', ',').strip()

    with open(out, 'w', encoding='utf-8', newline='') as fh:
        fh.write(';'.join(HK_BY_HEADER) + '\r\n')
        while True:
            if cancel and cancel():
                raise Cancelled()
            data = _ogc_get_json(http, _ogc_page_url(entry, offset))
            feats = data.get('features') or []
            total = int(data.get('numberMatched') or total or 0)
            for f in feats:
                p = f.get('properties') or {}
                g = f.get('geometry') or {}
                strasse, hn = val(p, 'strasse'), val(p, 'hausnummer')
                if not strasse or not hn or g.get('type') != 'Point':
                    continue
                lon, lat = g['coordinates'][:2]
                x, y = _utm_forward(lat, lon, 32)
                gem = val(p, 'gemeinde')
                zus = val(p, 'hausnr_zusatz')
                for hnr, adz in split_housenumbers(hn + zus):
                    key = (strasse.lower(), hnr, adz.lower(), gem)
                    if key in seen:
                        continue
                    seen.add(key)
                    row = ['N', str(f.get('id', ''))[:16], '', landschl, bundesland, '', '', '', '',
                           '', gem, '', '', '', strasse, hnr, adz, '32', f'{x:.2f}', f'{y:.2f}',
                           val(p, 'plz'), gem, '', '']
                    line = ';'.join(row) + '\r\n'
                    fh.write(line)
                    sha.update(line.encode('utf-8'))
                    n_ok += 1
            offset += len(feats)
            if progress:
                txt = (f'{_n(offset)} von {_n(total)} Adressen geladen …' if total
                       else f'{_n(offset)} Adressen geladen …')
                progress(offset, total, txt)
            if len(feats) < limit or (total and offset >= total):
                break
    if total and offset < total:
        raise SourceError(f'Der Download war unvollständig ({_n(offset)} von {_n(total)} Adressen). '
                          'Der bisherige Bestand bleibt erhalten — bitte später erneut versuchen.',
                          detail=f'ogcapi {entry["url"]}: {offset} von {total}')
    digest = sha.hexdigest()
    _detail(f'{bundesland}: {n_ok} Adressen aus {offset} Objekten, SHA-256 {digest}')
    if prev_meta and not force and prev_meta.get('sha256') == digest:
        _unlink(out)
        return {'status': 'unchanged', 'files': [], 'format': 'hkde', 'entry': entry, 'meta': None,
                'text': 'Keine neuen Daten seit dem letzten Import.'}
    _log(f'{_n(n_ok)} Adressen geladen.')
    meta = {'url': entry['url'], 'etag': None, 'last_modified': None, 'sha256': digest,
            'dateien': os.path.basename(out), 'heruntergeladen_am': _now_iso(),
            'quelle': entry.get('quelle_name', '')}
    return {'status': 'downloaded', 'files': [out], 'format': 'hkde', 'entry': entry, 'meta': meta,
            'text': ''}


GAIALIGHT_BBOX = '-500000,5000000,1500000,6500000'     # all of Germany in UTM 32


def gaialight_latest(entry, http):
    """Newest dataset of a GaiaLight download service (e.g. Schleswig-Holstein,
    object hk_alkis): dict with bestand, id, datum — from _ajax/overview.php."""
    base = entry['url'].rstrip('/')
    q = urllib.parse.urlencode({'type[]': entry['objekt'], 'crs': entry.get('crs') or 'EPSG:25832',
                                'bbox': entry.get('bbox') or GAIALIGHT_BBOX})
    _s, _h, body = http.get(f'{base}/_ajax/overview.php?{q}')
    try:
        data = json.loads(body.decode('utf-8'))
        feats = ((data.get('result') or {}).get('features')) or []
    except (ValueError, AttributeError) as e:
        raise SourceError('Die Übersicht des Downloaddienstes ist nicht lesbar.',
                          detail=f'overview.php nicht lesbar: {base}') from e
    best = None
    for f in feats:
        p = (f or {}).get('properties') or {}
        if p.get('bestand') and p.get('ogc_fid') is not None:
            cand = {'bestand': str(p['bestand']), 'id': p['ogc_fid'], 'datum': str(p.get('a_datum') or '')}
            if best is None or (cand['datum'], cand['bestand']) > (best['datum'], best['bestand']):
                best = cand
    if not best:
        raise SourceError(_SOURCE_CHANGED, detail=f'Keine Bestände für {entry["objekt"]} in {base}')
    return best


def _update_gaialight(bundesland, entry, http, cache, prev_meta, force, progress, _log, _detail,
                      cancel):
    """GaiaLight download service: determine the newest dataset, start a job,
    wait until the file has been assembled, then download it. Unchanged if
    the dataset is the same as at the last import (then no job is started)."""
    import time
    base = entry['url'].rstrip('/')
    best = gaialight_latest(entry, http)
    kennung = f'gaialight:{best["bestand"]}'
    _detail(f'{bundesland}: neuester Bestand {best["bestand"]} vom {best["datum"]}')
    if prev_meta and not force and prev_meta.get('etag') == kennung:
        return {'status': 'unchanged', 'files': [], 'format': entry['format'], 'entry': entry,
                'meta': None, 'text': 'Keine neuen Daten seit dem letzten Import.'}
    _log(f'Quelle: {entry.get("quelle_name", "")} · Lizenz {entry.get("lizenz", "?")}')
    q = urllib.parse.urlencode({'action': 'start', 'type': entry['objekt'],
                                'file[]': best['bestand'] + '.zip', 'id[]': best['id']})
    _s, _h, body = http.get(f'{base}/multi.php?{q}')
    try:
        job = json.loads(body.decode('utf-8'))
        job_id = job['id']
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError('Der Downloaddienst hat den Auftrag nicht angenommen. Bitte später erneut '
                          'versuchen.', detail=f'multi.php start: {body[:300]!r}') from e
    _log('Die Datei wird beim Landesamt zusammengestellt …')
    url, t0 = None, time.time()
    while time.time() - t0 < 900:
        for _ in range(10):                       # wait 5 s, checking for cancellation
            if cancel and cancel():
                raise Cancelled()
            time.sleep(0.5)
        if progress:
            progress(0, 0, f'Datei wird zusammengestellt … {int(time.time() - t0)} s')
        _s, _h, body = http.get(f'{base}/multi.php?' + urllib.parse.urlencode(
            {'action': 'status', 'job': job_id}))
        try:
            st = json.loads(body.decode('utf-8'))
        except ValueError:
            continue
        if st.get('status') == 'done' and st.get('downloadUrl'):
            url = st['downloadUrl']
            break
        if st.get('status') not in ('wait', 'running', 'work', None):
            raise SourceError('Der Downloaddienst konnte die Datei nicht zusammenstellen. Bitte '
                              'später erneut versuchen.', detail=f'multi.php status: {st}')
    if not url:
        raise SourceError('Der Downloaddienst hat die Datei nicht rechtzeitig bereitgestellt. Bitte '
                          'später erneut versuchen.', detail=f'Auftrag {job_id} nach 15 Minuten offen')

    def _p(done, total):
        if progress:
            progress(done, total, f'Lade Daten: {_mb(done)} MB' + (f' von {_mb(total)} MB' if total else '')
                     + ' …')
    info = download_file(http, url, cache, force=True, progress=_p, cancel=cancel)
    _log(f'Download abgeschlossen ({_mb(info["size"])} MB, Datenstand {_datum_de(best["datum"][:10])}).')
    files = extract_data_files(info['path'], cache, entry.get('entpacken'))
    _detail('Datendateien: ' + ', '.join(os.path.basename(f) for f in files))
    stand = best['datum'][:10]
    lm = None
    if _iso_date(stand):
        lm = formatdate(datetime.strptime(stand, '%Y-%m-%d').replace(
            tzinfo=timezone.utc).timestamp(), usegmt=True)
    meta = {'url': entry['url'], 'etag': kennung, 'last_modified': lm, 'sha256': info.get('sha256'),
            'dateien': ';'.join(os.path.basename(f) for f in files),
            'heruntergeladen_am': _now_iso(), 'quelle': entry.get('quelle_name', '')}
    return {'status': 'downloaded', 'files': files, 'format': entry['format'], 'entry': entry,
            'meta': meta, 'text': ''}


def _update_index_geojson(bundesland, entry, http, prev_meta, force, _log, _detail):
    """Many files (one per district/Kreis): only check whether one has changed
    (conditional request with the ETag of the last download per file). Download
    and conversion happen only during the import, file by file (result['remote']),
    so that never all files are on disk at the same time."""
    urls = index_geojson_urls(entry, http)
    _detail(f'{bundesland}: {len(urls)} Dateien im Index')
    prev = {}
    if prev_meta and str(prev_meta.get('etag') or '').startswith('{'):
        try:
            prev = json.loads(prev_meta['etag'])
        except ValueError:
            prev = {}
    if prev and not force and set(prev) == set(urls):
        geaendert = 0
        for u in urls:
            status, _h, _b = http.get(u, headers={'If-None-Match': prev[u]}, max_bytes=1)
            if status != 304:
                geaendert += 1
        if not geaendert:
            return {'status': 'unchanged', 'files': [], 'format': entry['format'],
                    'entry': entry, 'meta': None,
                    'text': 'Keine neuen Daten seit dem letzten Import.'}
        _log(f'Neue Daten bei {geaendert} von {len(urls)} Dateien.')
    _log(f'Quelle: {entry.get("quelle_name", "")} · Lizenz {entry.get("lizenz", "?")}')
    if entry.get('dauer'):
        _log(f'Download und Umwandlung dauern {entry["dauer"]}.')
    meta = {'url': entry['url'], 'etag': None, 'last_modified': None, 'sha256': None,
            'dateien': f'{len(urls)} Dateien', 'heruntergeladen_am': _now_iso(),
            'quelle': entry.get('quelle_name', '')}
    return {'status': 'downloaded', 'files': [], 'remote': urls, 'format': entry['format'],
            'entry': entry, 'meta': meta, 'text': ''}


def manual_text(bundesland, entry):
    """Hint for states without a registered source."""
    entry = entry or {}
    grund = (entry.get('grund') or '').strip()
    return (f'Für {bundesland} ist noch keine Quelle eingetragen.' + (f' {grund}' if grund else '')
            + f' Bitte die Datei über {entry.get("url") or "das Landesportal"} herunterladen und '
            'auf der Detailseite des Bundeslands als Datei eintragen.')


def file_signature(path):
    """(identifier, modification time as HTTP date, size) of a file — used to
    detect whether a registered file has been replaced since the last import."""
    st = os.stat(path)
    return f'datei:{st.st_size}:{int(st.st_mtime)}', formatdate(st.st_mtime, usegmt=True), st.st_size


_FILE_MUSTER = {'hkde': r'\.(txt|csv)$', 'gml': r'\.(gml|xml)$',
                'shp': r'\.(shp|shx|dbf|prj|cpg)$', 'osm': r'\.osm\.pbf$'}


def _update_from_file(bundesland, entry, db_path, prev_meta, force, log, detail):
    """Registered file: import it if it is new or has changed since the last
    import (size/modification time). Zip files are extracted into a separate
    subfolder of the cache folder (temp_dir) — the user's file itself is never
    modified or deleted."""
    _log    = log or (lambda m: None)
    _detail = detail or (lambda m: None)
    path = entry['pfad']
    if not os.path.isfile(path):
        raise SourceError(f'Die eingetragene Datei {os.path.basename(path)} wurde nicht gefunden. '
                          'Bitte auf der Detailseite des Bundeslands eine vorhandene Datei wählen.',
                          detail=f'Datei fehlt: {path}')
    etag, lm, size = file_signature(path)
    if prev_meta and not force and prev_meta.get('url') == path and prev_meta.get('etag') == etag:
        return {'status': 'unchanged', 'files': [], 'format': entry['format'],
                'entry': entry, 'meta': None,
                'text': 'Die Datei ist seit dem letzten Import unverändert.'}
    _log(f'Datei: {os.path.basename(path)} ({_mb(size)} MB, Stand {format_last_modified(lm)})')
    _detail(f'{bundesland}: eingetragene Datei {path}')
    temp_dir = None
    files = [path]
    if zipfile.is_zipfile(path):
        temp_dir = os.path.join(cache_dir_for(db_path, bundesland), '_entpackt')
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        files = extract_data_files(path, temp_dir,
                                   entry.get('entpacken') or _FILE_MUSTER.get(entry['format']))
        if entry['format'] == 'shp':
            files = [f for f in files if f.lower().endswith('.shp')]
            if not files:
                raise SourceError('Im Zip wurde kein Shapefile gefunden.')
        _detail('Entpackt: ' + ', '.join(os.path.basename(f) for f in files))
    meta = {'url': path, 'etag': etag, 'last_modified': lm, 'sha256': None,
            'dateien': ';'.join(os.path.basename(f) for f in files),
            'heruntergeladen_am': _now_iso(), 'quelle': entry.get('quelle_name', '')}
    return {'status': 'downloaded', 'files': files, 'format': entry['format'],
            'entry': entry, 'meta': meta, 'text': '', 'temp_dir': temp_dir}


def test_source(entry, http=None, user_agent_version='dev'):
    """Check a source without importing anything (for the source editor).
    Returns (ok, text)."""
    try:
        validate_catalog({'sources': {'Test': entry}})
    except Exception as e:
        return False, f'Eintrag ungültig: {e}'
    if entry.get('typ') == 'gaialight':
        try:
            ua = f'QGIS-GISAkademieGeocoder/{user_agent_version} (+https://gis-akademie.de)'
            best = gaialight_latest(entry, http or Http(ua, timeout=30))
        except SourceError as e:
            return False, str(e)
        return True, f'Neuester Bestand: {best["bestand"]} vom {_datum_de(best["datum"][:10])}'
    if entry.get('typ') == 'index_geojson':
        try:
            ua = f'QGIS-GISAkademieGeocoder/{user_agent_version} (+https://gis-akademie.de)'
            urls = index_geojson_urls(entry, http or Http(ua, timeout=30))
        except SourceError as e:
            return False, str(e)
        return True, f'{len(urls)} Dateien in der Dateiliste, z.B. {_file_name_from_url(urls[0])}'
    if is_file_source(entry):
        if not os.path.isfile(entry['pfad']):
            return False, 'Die Datei wurde nicht gefunden.'
        _etag, lm, size = file_signature(entry['pfad'])
        return True, f'Datei vorhanden · {_mb(size)} MB · Stand {format_last_modified(lm)}'
    if not is_automatic(entry):
        return False, 'Manuelle Quellen werden nicht abgerufen.'
    ua = f'QGIS-GISAkademieGeocoder/{user_agent_version} (+https://gis-akademie.de)'
    http = http or Http(ua, timeout=30)
    try:
        if entry['typ'] == 'wfs':
            typ = entry['typnamen'][-1]
            url = (f"{entry['url']}{'&' if '?' in entry['url'] else '?'}"
                   + urllib.parse.urlencode({'service': 'WFS', 'version': '2.0.0',
                                             'request': 'GetFeature', 'typeNames': typ,
                                             'resultType': 'hits'}))
            _s, _h, body = http.get(url, max_bytes=20000)
            m = re.search(rb'numberMatched="([^"]+)"', body)
            if not m:
                return False, 'WFS antwortet, aber ohne numberMatched — Typname prüfen.'
            return True, f'WFS erreichbar: {m.group(1).decode()} Objekte vom Typ {typ}.'
        urls = resolve_download_urls(entry, http)
        _s, hdrs, _b = http.get(urls[0], max_bytes=4)
        size = hdrs.get('content-length')
        stand = format_last_modified(hdrs.get('last-modified'))
        txt = f'{len(urls)} Datei(en) gefunden: ' + ', '.join(_file_name_from_url(u) for u in urls[:5])
        if size:
            txt += f' · {_mb(int(size))} MB'
        if stand:
            txt += f' · Stand {stand}'
        return True, txt
    except SourceError as e:
        return False, str(e)
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def format_last_modified(value):
    """'Wed, 25 Mar 2026 08:32:06 GMT' -> '25.03.2026' (for display)."""
    if not value:
        return ''
    try:
        return parsedate_to_datetime(value).strftime('%d.%m.%Y')
    except Exception:
        return value
