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

import contextlib
import os
import re
import json
import html
import shutil
import hashlib
import zipfile
import http.client
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

FORMATS = ('hkde', 'gml', 'shp', 'osm', 'alkis', 'geojson')
# osm: OpenStreetMap extract (.osm.pbf); alkis: ALKIS GeoPackage with buildings and land parcels
_FILE_FORMATS = ('hkde', 'gml', 'shp', 'osm')


class SourceType:
    """Everything the plugin knows about one fetch type — the single place that
    validation, download, test and the source editor use:
      label     — name in the user interface
      formats   — data formats this type can deliver to the import
      automatic — fetched from the internet without user intervention
      needs     — required catalog fields: ((field, …), message), …
      check     — further validation check(bl, entry) (optional)
      urls      — urls(entry, http) → file URLs, for types that deliver files
      update    — update(ctx) → result, for types with their own fetch logic
      test      — test(entry, http) → (ok, text) for „Quelle prüfen“
    The functions are assigned at the end of the module."""

    def __init__(self, label, formats, automatic=True, needs=()):
        self.label, self.formats, self.automatic, self.needs = label, formats, automatic, needs
        self.check = self.urls = self.update = self.test = None


SOURCE_TYPES = {
    'datei':         SourceType('Datei auf diesem Rechner', _FILE_FORMATS, automatic=False,
                                needs=((('pfad',), 'Bitte eine Datei auswählen.'),)),
    'direct':        SourceType('Feste Datei-URL', _FILE_FORMATS),
    'index_json':    SourceType('Verzeichnisindex (index.json)', _FILE_FORMATS,
                                needs=((('datei_muster',), 'Bitte ein Dateiname-Muster angeben.'),)),
    'page_link':     SourceType('Link auf einer Webseite (Regex)', _FILE_FORMATS,
                                needs=((('link_muster',), 'Bitte ein Link-Muster angeben.'),)),
    'atom':          SourceType('Atom-Feed (Regex)', _FILE_FORMATS,
                                needs=((('link_muster',), 'Bitte ein Link-Muster angeben.'),)),
    'rss':           SourceType('RSS-Feed (Regex)', _FILE_FORMATS,
                                needs=((('link_muster',), 'Bitte ein Link-Muster angeben.'),)),
    'ckan':          SourceType('CKAN-Datenkatalog (API)', _FILE_FORMATS,
                                needs=((('abfrage', 'ressource_muster'),
                                        'Bitte CKAN-Abfrage und Ressourcen-Muster angeben.'),)),
    'wfs':           SourceType('WFS 2.0 (seitenweise GML)', ('gml',),
                                needs=((('typnamen',), 'Bitte die WFS-Typnamen angeben.'),)),
    'index_geojson': SourceType('Dateiliste als GeoJSON (je Kreis eine Datei)', ('alkis',)),
    'gaialight':     SourceType('GaiaLight-Downloaddienst (Auftrag)', ('hkde', 'gml', 'shp'),
                                needs=((('objekt',), 'Bitte das Objekt des Downloaddienstes '
                                        'angeben (z.B. hk_alkis).'),)),
    'ogcapi':        SourceType('OGC API Features (GeoJSON-Seiten)', ('geojson',)),
    'manuell':       SourceType('manuell', FORMATS, automatic=False,
                                needs=((('grund',), 'grund fehlt (warum manuell?)'),)),
}
FILE_TYPE   = 'datei'      # registered file on this computer (path instead of URL)
MANUAL_TYPE = 'manuell'
# Derived views (used by the user interface and older callers)
TYPE_LABELS     = {k: t.label for k, t in SOURCE_TYPES.items()}
TYPE_FORMATS    = {k: t.formats for k, t in SOURCE_TYPES.items()}
AUTOMATIC_TYPES = tuple(k for k, t in SOURCE_TYPES.items() if t.automatic)
EDITABLE_TYPES  = tuple(k for k in SOURCE_TYPES if k != MANUAL_TYPE)

CHUNK = 1024 * 256

DEFAULT_USER_AGENT = 'QGIS-GISAkademieGeocoder/{version} (+https://gis-akademie.de)'

# Class for network access. In QGIS the plugin sets qgis_network.QgisHttp (proxy,
# certificates and authentication of QGIS); outside QGIS (tests, tools) the
# urllib variant Http below is used.
_HTTP_CLASS = None


def set_http_class(cls):
    global _HTTP_CLASS
    _HTTP_CLASS = cls


def make_http(user_agent, timeout=60):
    return (_HTTP_CLASS or Http)(user_agent, timeout)


class Cancelled(Exception):
    """Cancelled by the user."""


class SourceError(Exception):
    """Source not retrievable / catalog invalid. str(e) is worded for end
    users; e.detail contains technical details (URL, pattern …) for the
    QGIS message log. e.kind: 'voruebergehend' (network, server busy or in
    maintenance – try again later), 'geaendert' (file no longer there – the
    source has probably changed), 'konfiguration' (the catalog entry does not
    fit the service) or 'fehler'."""

    def __init__(self, message, detail=None, kind='fehler', code=None, headers=None):
        super().__init__(message)
        self.detail = detail
        self.kind = kind
        self.code = code                 # HTTP status for server errors
        self.headers = headers or {}     # response headers (e.g. Retry-After)


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


def _http_error(code, url, headers=None):
    host = _host(url)
    kind = 'fehler'
    if code in (404, 410):
        msg = (f'Die Datei ist auf dem Server {host} nicht (mehr) vorhanden – die Quelle hat '
               'sich vermutlich geändert. Bitte im Reiter „Datenbank & Quellen“ prüfen oder der '
               'GIS-Akademie melden.')
        kind = 'geaendert'
    elif code in (401, 403):
        msg = f'Der Server {host} verweigert den Zugriff (Fehler {code}).'
        kind = 'geaendert'
    elif code == 429:
        msg = f'Der Server {host} meldet zu viele Anfragen. Bitte später erneut versuchen.'
        kind = 'voruebergehend'
    elif code >= 500:
        msg = f'Der Server {host} ist derzeit gestört (Fehler {code}). Bitte später erneut versuchen.'
        kind = 'voruebergehend'
    else:
        msg = f'Der Server {host} meldet Fehler {code}.'
    return SourceError(msg, detail=f'HTTP {code} bei {url}', kind=kind, code=code,
                       headers={k.lower(): v for k, v in (headers or {}).items()})


def _net_error(url, reason):
    return SourceError(f'Keine Verbindung zum Server {_host(url)}. Bitte die Internetverbindung '
                       'prüfen und später erneut versuchen.',
                       detail=f'Netzwerkfehler bei {url}: {reason}', kind='voruebergehend')


# Errors while connecting or while reading the response body (timeouts,
# connection resets, truncated responses) — urllib only wraps some of them.
_NET_ERRORS = (OSError, http.client.HTTPException)


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
    _write_json(path, {'version': 1, 'sources': sources})


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


def _schema_version(value):
    """Catalog format version as int ('1', 1, '1.0' → 1); unreadable → treated
    as too new, so that an unexpected list is never used."""
    try:
        return int(float(str(value if value not in (None, '') else 1)))
    except (TypeError, ValueError):
        return CATALOG_SCHEMA + 1


def _valid_online_entry(bl, entry):
    """Check an entry of an online list: valid as in the bundled catalog,
    but only internet sources or "manuell" and only http(s) URLs — a list
    from the internet must never point to files on this computer (neither
    the main source nor the fallback source)."""
    if not isinstance(entry, dict) or entry.get('typ') == FILE_TYPE:
        raise SourceError(f'{bl}: Typ in der Online-Liste nicht erlaubt')
    for e in (entry, entry.get('ausweich') or {}):
        if e is not entry and not e:
            continue
        if not isinstance(e, dict) or e.get('typ') == FILE_TYPE:
            raise SourceError(f'{bl}: Typ in der Online-Liste nicht erlaubt')
        url = str(e.get('url', ''))
        if not url.lower().startswith(('https://', 'http://')):
            raise SourceError(f'{bl}: keine http(s)-Adresse')
    validate_catalog({'sources': {bl: entry}})


def _is_valid_online_entry(bl, entry):
    try:
        _valid_online_entry(bl, entry)
    except Exception:
        return False
    return True


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
    if _schema_version(online.get('version')) > CATALOG_SCHEMA:
        cat['_fehler'].append('Die online verfügbare Quellenliste braucht eine neuere Version des '
                              'Plugins — es gilt die mitgelieferte Liste.')
        return
    ersetzt = []
    for bl, entry in online['sources'].items():
        if not _is_valid_online_entry(bl, entry):
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
        t = SOURCE_TYPES.get(typ)
        if t is None:
            raise SourceError(f'{bl}: unbekannter typ {typ!r}')
        if e.get('format') not in FORMATS:
            raise SourceError(f'{bl}: unbekanntes format {e.get("format")!r}')
        if e['format'] not in t.formats:
            raise SourceError(f'{bl}: Das Datenformat {e["format"]!r} passt nicht zur Bezugsart '
                              f'{t.label!r}.')
        if typ != FILE_TYPE and not e.get('url'):
            raise SourceError(f'{bl}: Bitte eine URL angeben.')
        for keys, message in t.needs:
            if not all(e.get(k) for k in keys):
                raise SourceError(f'{bl}: {message}')
        if t.check:
            t.check(bl, e)
        if e.get('format') == 'shp' and typ != MANUAL_TYPE:
            _check_felder(bl, e, 'Bitte im Shapefile mindestens die Felder für Straße und '
                                 'Hausnummer zuordnen.')
        if e.get('ausweich'):
            if not isinstance(e['ausweich'], dict) or e['ausweich'].get('typ') not in AUTOMATIC_TYPES:
                raise SourceError(f'{bl}: Die Ausweichquelle muss eine Internetquelle sein.')
            validate_catalog({'sources': {f'{bl} (Ausweichquelle)': e['ausweich']}})
        for key, top in (('vorlage_tage', 31), ('vorlage_jahre', 5), ('vorlage_nr', 52)):
            if key in e and not (isinstance(e[key], int) and 1 <= e[key] <= top):
                raise SourceError(f'{bl}: {key} muss eine ganze Zahl von 1 bis {top} sein')
        for key in ('datei_muster', 'link_muster', 'ressource_muster', 'entpacken'):
            if e.get(key):
                try:
                    re.compile(e[key])
                except re.error as err:
                    raise SourceError(f'{bl}: Das Muster „{e[key]}“ ist kein gültiger '
                                      f'regulärer Ausdruck ({err}).')


def _check_felder(bl, e, message='Bitte die Felder für Straße und Hausnummer angeben.'):
    felder = e.get('felder') or {}
    if not (felder.get('strasse') and felder.get('hausnummer')):
        raise SourceError(f'{bl}: {message}')


def _check_wfs(bl, e):
    if e.get('abruf', 'seiten') not in ('seiten', 'kacheln'):
        raise SourceError(f'{bl}: abruf muss "seiten" oder "kacheln" sein')
    if e.get('abruf') == 'kacheln':
        bb = e.get('kachel_bbox')
        if not e.get('kachel_crs') or not (isinstance(bb, list) and len(bb) == 4):
            raise SourceError(f'{bl}: kacheln braucht kachel_crs und kachel_bbox [x0,y0,x1,y1]')


def user_agent(version='dev', catalog=None):
    """User-Agent with contact address from the catalog (usage rule of the
    data providers) — used for every request of the plugin."""
    if catalog is None:
        try:
            catalog = load_catalog(include_user=False, include_online=False)
        except Exception:
            catalog = {}
    return (catalog.get('user_agent') or DEFAULT_USER_AGENT).replace('{version}', version)


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

    def _open(self, url, headers):
        """Open the connection; returns the response or (304, headers)."""
        if urllib.parse.urlparse(url).scheme.lower() not in ('http', 'https'):
            raise SourceError('Es werden nur Internetadressen (http/https) abgerufen.',
                              detail=f'Adresse abgelehnt: {url}', kind='konfiguration')
        try:
            # nosec: only http/https reaches urlopen (checked above)
            return urllib.request.urlopen(  # nosec B310
                self._request(url, headers), timeout=self.timeout)
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            if e.code == 304:
                return 304, hdrs
            raise _http_error(e.code, url, hdrs) from e
        except urllib.error.URLError as e:
            raise _net_error(url, e.reason) from e
        except _NET_ERRORS as e:
            raise _net_error(url, e) from e

    def get(self, url, headers=None, max_bytes=None):
        r = self._open(url, headers)
        if isinstance(r, tuple):
            return r[0], r[1], b''
        with r:
            try:
                body = r.read(max_bytes) if max_bytes else r.read()
            except _NET_ERRORS as e:          # timeout, reset, truncated body
                raise _net_error(url, e) from e
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body

    def download(self, url, dest, headers=None, progress=None, cancel=None):
        """Streams url to dest (via a temporary file). Returns
        (status, headers_dict). On 304 nothing is written. A download that
        ends early (fewer bytes than announced) is an error — never a file."""
        r = self._open(url, headers)
        if isinstance(r, tuple):
            return r
        tmp = dest + '.part'
        try:
            with r:
                hdrs  = {k.lower(): v for k, v in r.headers.items()}
                total = int(hdrs.get('content-length') or 0)
                done  = 0
                sha   = hashlib.sha256()
                with open(tmp, 'wb') as f:
                    while True:
                        if cancel and cancel():
                            raise Cancelled()
                        try:
                            chunk = r.read(CHUNK)
                        except _NET_ERRORS as e:
                            raise _net_error(url, e) from e
                        if not chunk:
                            break
                        f.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                if total and done < total:
                    raise SourceError(
                        f'Der Download von {_host(url)} wurde vorzeitig beendet ({_mb(done)} von '
                        f'{_mb(total)} MB). Bitte später erneut versuchen – der bisherige Bestand '
                        'bleibt erhalten.',
                        detail=f'Verbindung nach {done} von {total} Bytes beendet: {url}',
                        kind='voruebergehend')
                hdrs['_sha256'] = sha.hexdigest()
                hdrs['_size']   = str(done)
            os.replace(tmp, dest)
            return r.status, hdrs
        except BaseException:
            _unlink(tmp)
            raise


def _unlink(path):
    with contextlib.suppress(OSError):
        os.unlink(path)


# ── URL resolution ───────────────────────────────────────────────────────────

def _absolute(base, link):
    # Links found in HTML/Atom are entity-encoded ('&amp;' between query parameters)
    return urllib.parse.urljoin(base, html.unescape(link.strip()))


TEMPLATE_DAYS  = 2      # {datum}: today and yesterday (catalog field 'vorlage_tage')
TEMPLATE_YEARS = 2      # {jahr}: current and previous year ('vorlage_jahre')
TEMPLATE_NR    = 12     # {nr}: releases 12 down to 1 ('vorlage_nr')


def url_template_candidates(url, today=None, tage=TEMPLATE_DAYS, jahre=TEMPLATE_YEARS,
                            nr=TEMPLATE_NR):
    """Candidates for a URL with placeholders, newest first:
      {datum} — today's date YYYYMMDD, then the days before (in case the
                server does not have today's folder yet),
      {jahr}/{nr} — data release, e.g. "-2026-01": current and previous
                year(s), number nr down to 1 (two digits).
    The look-back ranges come from the catalog entry (vorlage_tage,
    vorlage_jahre, vorlage_nr), so they can be changed with the online list.
    Example Hessen: …/downloadcenter/{datum}/…/Hauskoordinaten…-{jahr}-{nr}.zip"""
    from datetime import date, timedelta
    today = today or date.today()
    tage = ([today - timedelta(days=d) for d in range(max(1, int(tage)))]
            if '{datum}' in url else [today])
    jahre = [today.year - k for k in range(max(1, int(jahre)))]
    if '{jahr}' in url or '{nr}' in url:
        fassungen = ([(j, n) for j in jahre for n in range(max(1, int(nr)), 0, -1)]
                     if '{nr}' in url else [(j, 1) for j in jahre])
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


def _is_html(headers):
    return 'text/html' in str((headers or {}).get('content-type', '')).lower()


def resolve_url_template(url, http, today=None, entry=None):
    """First existing URL from url_template_candidates — checked by fetching
    the first bytes (some servers do not allow HEAD requests). A candidate
    counts as missing on 404/403 or when the server answers with a web page
    instead of a file; a network problem or server error ends the search at
    once (it would affect every candidate)."""
    tried = []
    entry = entry or {}
    for cand in url_template_candidates(url, today,
                                        entry.get('vorlage_tage', TEMPLATE_DAYS),
                                        entry.get('vorlage_jahre', TEMPLATE_YEARS),
                                        entry.get('vorlage_nr', TEMPLATE_NR)):
        try:
            status, h, _b = http.get(cand, headers={'Range': 'bytes=0-3'}, max_bytes=4)
        except SourceError as e:
            if e.kind == 'voruebergehend':
                raise
            tried.append(cand)
            continue
        if status in (200, 206) and not _is_html(h):
            return cand
        tried.append(cand)
    raise SourceError(_SOURCE_CHANGED, kind='geaendert',
                      detail=f'Keine der {len(tried)} Adressen erreichbar, Muster {url}')


def index_geojson_urls(entry, http):
    """File URLs from an index in GeoJSON format (e.g. LGLN Niedersachsen:
    one feature per district/Kreis, field 'zip'). Umlauts in the URLs are encoded."""
    feld = entry.get('url_feld') or 'zip'
    _s, _h, body = http.get(entry['url'])
    try:
        data = json.loads(body.decode('utf-8'))
        feats = (data.get('features') if isinstance(data, dict) else None) or []
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
        raise SourceError(_SOURCE_CHANGED, kind='geaendert', detail=f'Keine Einträge im Feld {feld!r} in {entry["url"]}')
    return sorted(urls)


def resolve_download_urls(entry, http):
    """Returns the list of file URLs to download for an entry (types that
    deliver files: direct, index_json, page_link/atom/rss, ckan)."""
    urls = SOURCE_TYPES[entry['typ']].urls
    if urls is None:
        raise SourceError(f'Typ {entry["typ"]!r} hat keine Datei-URLs')
    return urls(entry, http)


def _urls_direct(entry, http):
    url = entry['url']
    return [resolve_url_template(url, http, entry=entry)] if '{' in url else [url]


def _urls_index_json(entry, http):
    url = entry['url']
    _status, _, body = http.get(url)
    try:
        idx = json.loads(body.decode('utf-8'))
        if not isinstance(idx, dict):
            raise ValueError('kein Objekt')
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
        raise SourceError(_SOURCE_CHANGED, kind='geaendert', detail=f'Keine Datei passend zu '
                          f'{entry["datei_muster"]!r} im Index {url}')
    return [_absolute(url, n) for n in sorted(set(names))]


def _urls_links(entry, http):
    """page_link / atom / rss: file links found with a regex."""
    url = entry['url']
    _status, _, body = http.get(url)
    text  = body.decode('utf-8', 'replace')
    links = re.findall(entry['link_muster'], text)
    links = [m if isinstance(m, str) else m[0] for m in links]   # findall: str or groups
    if not links:
        raise SourceError(_SOURCE_CHANGED, kind='geaendert', detail=f'Kein Link passend zu '
                          f'{entry["link_muster"]!r} auf {url}')
    seen, out = set(), []
    for link in links:
        a = _absolute(url, link)
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _urls_ckan(entry, http):
    url = entry['url']
    q = urllib.parse.urlencode({'q': entry['abfrage'], 'rows': entry.get('rows', 10),
                                'sort': 'metadata_modified desc'})
    _status, _, body = http.get(f'{url}?{q}')
    try:
        data = json.loads(body.decode('utf-8'))
        if not isinstance(data, dict):
            raise ValueError('kein Objekt')
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
        raise SourceError(_SOURCE_CHANGED, kind='geaendert', detail=f'Keine CKAN-Ressource passend zu '
                          f'{entry["ressource_muster"]!r}')
    return [best[1]]


# ── Download & extraction ────────────────────────────────────────────────────

def _file_name_from_url(url):
    name = os.path.basename(urllib.parse.urlparse(url).path) or 'download.bin'
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name)


def _url_key(url):
    """URL without the daily folder (/YYYYMMDD/) — e.g. Hessen: the same file is
    placed under a new date folder every day, the server returns the same ETag."""
    return re.sub(r'/\d{8}/', '/{datum}/', url or '')


def download_file(http, url, dest_dir, prev_meta=None, force=False,
                  progress=None, cancel=None, name=None):
    """Downloads url into dest_dir (as 'name', default: the URL's file name).
    prev_meta (dict with etag/last_modified of the last successful processing
    of this URL) controls the conditional request; force=True forces the
    download.

    Returns dict(status='ok'|'unchanged', path, etag, last_modified, size,
    sha256, url)."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name or _file_name_from_url(url))
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
    if _is_html(hdrs):
        # Maintenance or error page instead of the file (e.g. Thüringen, status 200/206)
        _unlink(dest)
        raise SourceError(f'Der Server {_host(url)} liefert derzeit keine Datei, sondern eine '
                          'Webseite (z.B. Wartung). Bitte später erneut versuchen – der bisherige '
                          'Bestand bleibt erhalten.', detail=f'text/html statt Datei bei {url}',
                          kind='voruebergehend')
    info = {'status': 'ok', 'path': dest, 'url': url,
            'etag': hdrs.get('etag'), 'last_modified': hdrs.get('last-modified'),
            'size': int(hdrs.get('_size') or 0), 'sha256': hdrs.get('_sha256')}
    # No ETag/Last-Modified from the server (or a new URL, e.g. a changing
    # access token in Sachsen), but the same content as last time?
    if (prev_meta and not force and prev_meta.get('sha256')
            and prev_meta['sha256'] == info['sha256']):
        info['status'] = 'unchanged'
    return info


def _safe_name(text):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', text).strip('_')


def extract_data_files(archive, dest_dir, muster=None, _tiefe=0, _used=None):
    """Extracts a zip and returns the data files (regex 'muster' applied to the
    file name; without a pattern all files). A zip inside the zip (e.g. Bremen:
    GEBAEUDEADRESSEN_SHP_FHB.zip contains GEBAEUDEADRESSEN_SHP_FHB_2026_08.zip)
    is extracted as well. Non-zip files are returned unchanged.
    Files are written flat (only base names — prevents zip slip); members
    with the same name from different folders get the folder as a prefix, so
    they never overwrite each other (shapefile parts of one folder keep a
    common prefix)."""
    if not zipfile.is_zipfile(archive):
        return [archive]
    used = set() if _used is None else _used
    pat = re.compile(muster, re.IGNORECASE) if muster else None
    out = []
    with zipfile.ZipFile(archive) as z:
        members = [i for i in z.infolist() if not i.is_dir()]
        names = [os.path.basename(i.filename) for i in members]
        clash = {n for n in names if names.count(n) > 1} | {n for n in names if n.lower() in used}
        for info, base in zip(members, names):
            inner_zip = base.lower().endswith('.zip') and _tiefe < 2
            if pat and not pat.search(base) and not inner_zip:
                continue
            name = base
            if base in clash:
                folder = _safe_name(os.path.dirname(info.filename).replace('/', '_'))
                name = f'{folder or "z"}__{base}'
                k = 2
                while name.lower() in used:
                    name = f'{folder or "z"}{k}__{base}'
                    k += 1
            used.add(name.lower())
            target = os.path.join(dest_dir, name)
            with z.open(info) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            if inner_zip and zipfile.is_zipfile(target):
                with contextlib.suppress(SourceError):
                    out.extend(extract_data_files(target, dest_dir, muster, _tiefe + 1, used))
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


def _id_pattern(typname):
    return re.compile(rb'<(?:\w+:)?' + _local(typname).encode() + rb'\b[^>]*?gml:id="([^"]+)"')


def _feature_ids(path, typname):
    """gml:id of all objects of a type in a GML file (without an XML parser —
    fast even for 40 MB pages). 'Address' does not match 'AddressLocator'."""
    with open(path, 'rb') as f:
        return _id_pattern(typname).findall(f.read())


def _page_stats(path, typname):
    """(number of wfs:member elements, gml:ids of the type) — one read per page."""
    with open(path, 'rb') as f:
        data = f.read()
    return len(_MEMBER_RE.findall(data)), _id_pattern(typname).findall(data)


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


def _fetch_paged(entry, typname, dest_dir, http, cancel, log, progress, expected, shas=None):
    count = int(entry.get('seitengroesse', 10000))
    safe  = typname.replace(':', '_')
    files, ids = [], set()
    start = page = 0
    while True:
        if cancel and cancel():
            raise Cancelled()
        dest = os.path.join(dest_dir, f'{safe}_{page:04d}.gml')
        _s, hdrs = http.download(wfs_page_url(entry['url'], typname, count, start), dest,
                                 cancel=cancel)
        n, page_ids = _page_stats(dest, typname)
        new = set(page_ids) - ids
        # Do NOT stop when n < count: some servers cap 'count' at their own
        # maximum. The type ends with an empty page — or with a page that
        # brings no new objects: a server that ignores startIndex (e.g.
        # Brandenburg) returns the first page again and again.
        if n == 0 or not new:
            _unlink(dest)
            if n and log:
                log(f'{typname}: Seite ab {start} enthält keine neuen Objekte – Ende')
            break
        files.append(dest)
        if shas is not None:
            shas.append((hdrs or {}).get('_sha256') or '')
        ids.update(new)
        if log:
            log(f'{typname}: Seite {page + 1}, {_n(n)} Objekte')
        if progress:
            progress(len(ids), expected or 0, _de(typname))
        page  += 1
        start += n
    _check_complete(typname, len(ids), expected, log)
    return files


def _fetch_tiled(entry, typname, dest_dir, http, cancel, log, progress, expected, shas=None):
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
        _s, hdrs = http.download(_wfs_url(entry['url'], typeNames=typname, count=limit, bbox=bbox),
                                 dest, cancel=cancel)
        if shas is not None:
            shas.append((hdrs or {}).get('_sha256') or '')
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


def _fetch_by_ids(entry, typname, ids, dest_dir, http, cancel, log, progress=None, shas=None):
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
        _s, hdrs = http.download(_wfs_url(entry['url'], resourceId=','.join(part)), dest,
                                 cancel=cancel)
        if shas is not None:
            shas.append((hdrs or {}).get('_sha256') or '')
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


def fetch_wfs(entry, dest_dir, http, progress=None, cancel=None, log=None, shas=None):
    """Fetch all feature types of the entry and save them as GML files.
    Returns the file list in import order (the GML import reads names/
    administrative units from ALL files in pass 1). shas (list) receives the
    SHA-256 of every file in the same order — for change detection without
    reading the files again."""
    os.makedirs(dest_dir, exist_ok=True)
    for old in os.listdir(dest_dir):
        if old.endswith('.gml'):
            _unlink(os.path.join(dest_dir, old))
    mode   = entry.get('abruf', 'seiten')
    tiled  = entry.get('kachel_typ', 'ad:Address')
    limit  = int(entry.get('max_objekte', 10000))
    types  = list(entry['typnamen'])
    if mode == 'kacheln' and tiled in types:      # addresses first: they provide the IDs
        types.remove(tiled)
        types.insert(0, tiled)
    files = []
    addr_files = []
    for typname in types:
        # Every type is checked for completeness against the server's count
        expected = wfs_hits(http, entry['url'], typname)
        if mode == 'kacheln' and typname == tiled:
            f = _fetch_tiled(entry, typname, dest_dir, http, cancel, log, progress, expected, shas)
            addr_files = f
        elif mode == 'kacheln' and expected > limit:
            ids = _referenced_ids(addr_files, typname)
            if log:
                log(f'{typname}: {_n(expected)} Objekte, zu viele für eine Antwort — lade die '
                    f'{_n(len(ids))} referenzierten per ID')
            f = _fetch_by_ids(entry, typname, ids, dest_dir, http, cancel, log, progress, shas)
        else:
            f = _fetch_paged(entry, typname, dest_dir, http, cancel, log, progress, expected, shas)
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


_NO_NEW_DATA = 'Keine neuen Daten seit dem letzten Import.'


def _unchanged(entry, text=_NO_NEW_DATA, fmt=None):
    return {'status': 'unchanged', 'files': [], 'format': fmt or entry['format'],
            'entry': entry, 'meta': None, 'text': text}


def _meta(entry, url=None, **fields):
    """source_meta fields of a download (url defaults to the catalog URL)."""
    meta = {'url': url or entry.get('url'), 'etag': None, 'last_modified': None, 'sha256': None,
            'dateien': '', 'heruntergeladen_am': _now_iso(), 'quelle': entry.get('quelle_name', '')}
    meta.update(fields)
    return meta


def _log_source(log, entry):
    log(f'Quelle: {entry.get("quelle_name", "")} · Lizenz {entry.get("lizenz", "?")}')


def _same_source(prev_meta, entry):
    """Did the last download (source_meta) come from this catalog entry? The
    lock after a download only applies to the same source — not after a
    fallback source or after the source was changed. Placeholders ({datum},
    {jahr}, {nr}) in the entry's URL match any value."""
    prev = _url_key(str(prev_meta.get('url') or ''))
    cur = str(entry.get('url') or entry.get('pfad') or '')
    if not prev or not cur:
        return True                    # older state without URL: keep the lock
    pat = re.escape(_url_key(cur))
    for key in ('{datum}', '{jahr}', '{nr}'):
        pat = pat.replace(re.escape(key), r'[^/]*')
    return re.fullmatch(pat, prev) is not None


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
    t = SOURCE_TYPES.get(entry.get('typ'))
    if t is None or (not t.automatic and t.update is None):
        return {'status': 'manual', 'files': [], 'format': entry.get('format'),
                'entry': entry, 'meta': None,
                'text': manual_text(bundesland, entry)}
    ctx = _Ctx(bundesland=bundesland, entry=entry, db_path=db_path, prev_meta=prev_meta,
               force=force, progress=progress, cancel=cancel,
               log=log or (lambda m: None), detail=detail or (lambda m: None))
    if not t.automatic:                      # file on this computer
        return t.update(ctx)

    ctx.http = http or make_http(user_agent(user_agent_version, cat))
    ctx.cache = cache_dir_for(db_path, bundesland)
    os.makedirs(ctx.cache, exist_ok=True)
    ctx.detail(f'{bundesland}: Quelle {entry.get("url")} (Typ {entry["typ"]}), Cache {ctx.cache}')

    # Lock after a download (min_abstand_tage): NOT a schedule — the plugin never
    # downloads on its own. When updating, the source is only skipped as long as
    # the last download is more recent. For sources without ETag/Last-Modified (WFS)
    # and for sources that change daily (OpenStreetMap extracts) — otherwise every
    # click would re-download the complete dataset (hundreds of MB). Only a
    # download from this same source counts.
    tage = entry.get('min_abstand_tage')
    if (tage and prev_meta and not force and prev_meta.get('heruntergeladen_am')
            and _same_source(prev_meta, entry)):
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
    return (t.update or _update_files)(ctx)


class _Ctx:
    """Everything a fetch function needs (see update_source)."""

    def __init__(self, **kw):
        self.http = self.cache = None
        self.__dict__.update(kw)


def _update_wfs(c):
    entry, progress, cancel, prev_meta, force = c.entry, c.progress, c.cancel, c.prev_meta, c.force
    http, cache, _log, _detail = c.http, c.cache, c.log, c.detail
    _log_source(_log, entry)
    _log('Lade Adressen vom Dienst der Landesvermessung'
         + (f' – das dauert {entry["dauer"]} …' if entry.get('dauer') else ' …'))

    def _wfs_progress(done, total, label):
        if progress:
            txt = (f'{_n(done)} von {_n(total)} {label} geladen …' if total
                   else f'{_n(done)} {label} geladen …')
            progress(done, total, txt)

    shas = []
    files = fetch_wfs(entry, cache, http, progress=_wfs_progress, cancel=cancel, log=_detail,
                      shas=shas)
    if cancel and cancel():
        raise Cancelled()
    size = sum(os.path.getsize(f) for f in files)
    # Checksum over the per-file checksums of the download (no second read)
    digest = hashlib.sha256('\n'.join(shas).encode('ascii')).hexdigest()
    if prev_meta and not force and prev_meta.get('sha256') == digest:
        return _unchanged(entry)
    _log(f'Download abgeschlossen ({_mb(size)} MB).')
    _detail(f'{len(files)} GML-Dateien, SHA-256 {digest}')
    meta = _meta(entry, sha256=digest, dateien=';'.join(os.path.basename(f) for f in files))
    return {'status': 'downloaded', 'files': files, 'format': entry['format'],
            'entry': entry, 'meta': meta, 'text': ''}


def _update_files(c):
    """Types that deliver files: resolve the URLs, download (conditionally),
    extract."""
    entry, progress, cancel, prev_meta, force = c.entry, c.progress, c.cancel, c.prev_meta, c.force
    http, cache, _log, _detail = c.http, c.cache, c.log, c.detail
    _log_source(_log, entry)
    if entry.get('dauer'):
        _log(f'Download und Import dauern {entry["dauer"]}.')
    urls = resolve_download_urls(entry, http)
    _detail(f'{len(urls)} Datei(en) ermittelt: ' + ', '.join(urls))
    all_files, infos, unchanged = [], [], True
    for i, url in enumerate(urls):
        if cancel and cancel():
            raise Cancelled()
        pm = prev_meta if (prev_meta and len(urls) == 1) else None
        # Several files: numbered names, so that equal names in different
        # URLs (e.g. …/download?id=1, ?id=2) never overwrite each other
        name = f'{i + 1:03d}_{_file_name_from_url(url)}' if len(urls) > 1 else None

        def _p(done, total, _i=i, _k=len(urls)):
            if progress:
                txt = (f'Lade Daten: {_mb(done)} MB' + (f' von {_mb(total)} MB' if total else '')
                       + (f' (Datei {_i + 1} von {_k})' if _k > 1 else '') + ' …')
                progress(done, total, txt)
        info = download_file(http, url, cache, prev_meta=pm, force=force,
                             progress=_p, cancel=cancel, name=name)
        infos.append(info)
        if info['status'] != 'unchanged':
            unchanged = False
            stand = format_last_modified(info.get('last_modified'))
            _log(f'Download abgeschlossen ({_mb(info["size"])} MB'
                 + (f', Datenstand {stand}' if stand else '') + ').')
    # Unchanged according to server/checksum: this state is already imported (source_meta
    # is in the same DB) — the file in the cache folder is not needed for that.
    if unchanged:
        return _unchanged(entry)

    used = set()
    for info in infos:
        all_files.extend(extract_data_files(info['path'], cache, entry.get('entpacken'), _used=used))
    _detail('Datendateien: ' + ', '.join(os.path.basename(f) for f in all_files))
    first = infos[0]
    meta  = _meta(entry, url=first['url'], etag=first.get('etag'),
                  last_modified=first.get('last_modified'), sha256=first.get('sha256'),
                  dateien=';'.join(os.path.basename(f) for f in all_files))
    return {'status': 'downloaded', 'files': all_files, 'format': entry['format'],
            'entry': entry, 'meta': meta, 'text': ''}


def fetch_online_catalog(http=None, user_agent_version='dev', url=None):
    """Fetch the online source list (conditionally via ETag) and cache it.
    Returns (status, text) with status 'neu' | 'aktuell' | 'zu_neu' | 'fehler'.
    Never raises — without network the previous list simply stays in effect."""
    try:
        return _fetch_online_catalog(http, user_agent_version, url)
    except Exception as e:
        return 'fehler', ('Die Quellenliste konnte online nicht geprüft werden — es gilt die bisherige. '
                          f'({type(e).__name__})')


def _fetch_online_catalog(http, user_agent_version, url):
    if not ONLINE_CATALOG_PATH:
        return 'fehler', 'Kein Speicherort für die Online-Quellenliste gesetzt.'
    try:
        base = load_catalog(include_user=False, include_online=False)
    except Exception as e:
        return 'fehler', f'Mitgelieferte Quellenliste nicht lesbar: {e}'
    alt = load_online_catalog() or {}
    # The address of the list comes from the newer of the two lists (a plugin
    # update may move it, a newer online list may announce a new one)
    newer = alt if _iso_date(alt.get('stand')) > _iso_date(base.get('stand')) else base
    url = url or newer.get('katalog_url') or base.get('katalog_url') or DEFAULT_CATALOG_URL
    http = http or make_http(user_agent(user_agent_version, base), timeout=30)
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
    if _schema_version(data.get('version')) > CATALOG_SCHEMA:
        return 'zu_neu', ('Die online verfügbare Quellenliste braucht eine neuere Version des Plugins. '
                          'Bitte das Plugin aktualisieren; bis dahin gilt die bisherige Liste.')
    if alt and _iso_date(data.get('stand')) < _iso_date(alt.get('stand')):
        # An older copy (e.g. from a cache on the way) never replaces a newer list
        return 'aktuell', _stand_text(base, alt)
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


def _ogc_page_url(entry, offset, limit=None):
    base = entry['url']
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}' + urllib.parse.urlencode(
        {'f': 'json', 'limit': int(limit or entry.get('seitengroesse') or 2500), 'offset': offset})


def _ogc_next_link(data, page_url):
    for link in data.get('links') or []:
        if isinstance(link, dict) and link.get('rel') == 'next' and link.get('href'):
            return urllib.parse.urljoin(page_url, link['href'])
    return None


def _update_ogcapi(bundesland, entry, http, cache, prev_meta, force, progress, _log, _detail,
                   cancel):
    """OGC API Features (e.g. Saarland): load all pages as GeoJSON and write
    them into an HK-BY address file (field mapping 'felder'). Change
    detection via the file's checksum (the service provides no ETag).

    Paging follows the 'next' links of the service (OGC API Features Part 1);
    only a service without links is paged by offset. A page that brings no new
    objects ends the download (protects against servers ignoring offset), and
    the number of unique objects is checked against numberMatched."""
    from .osm_convert import HK_BY_HEADER, split_housenumbers, hk_by_row, _clean, _LANDSCHL
    from .plz_resolver import _utm_forward
    felder = entry['felder']
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, 'adressen_ogcapi.txt')
    _log_source(_log, entry)
    _log('Lade Adressen vom Dienst der Landesvermessung'
         + (f' – das dauert {entry["dauer"]} …' if entry.get('dauer') else ' …'))
    landschl = _LANDSCHL.get(bundesland, '')
    sha, n_ok, total, offset = hashlib.sha256(), 0, 0, 0
    seen, ids = set(), set()
    limit = int(entry.get('seitengroesse') or 2500)

    def val(props, key):
        k = felder.get(key)
        v = props.get(k) if k else None
        return '' if v is None else _clean(str(v))

    url = _ogc_page_url(entry, 0)
    with open(out, 'w', encoding='utf-8', newline='') as fh:
        fh.write(';'.join(HK_BY_HEADER) + '\r\n')
        while url:
            if cancel and cancel():
                raise Cancelled()
            try:
                data = _ogc_get_json(http, url)
            except SourceError:
                if not (ids and total):
                    raise
                break        # a further page is not available — the count check decides below
            if not isinstance(data, dict):
                raise SourceError('Der Dienst hat keine lesbaren Daten geliefert. Bitte später '
                                  'erneut versuchen.', detail=f'keine FeatureCollection von {url}')
            feats = data.get('features') or []
            total = int(data.get('numberMatched') or total or 0)
            new = 0
            for f in feats:
                fid = f.get('id')
                fkey = fid if fid is not None else json.dumps(f, sort_keys=True)[:500]
                if fkey in ids:
                    continue
                ids.add(fkey)
                new += 1
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
                    row = hk_by_row(landschl, bundesland, '', gem, strasse, hnr, adz, '32', x, y,
                                    val(p, 'plz'), gem, ident=_clean(str(fid or ''))[:16])
                    line = ';'.join(row) + '\r\n'
                    fh.write(line)
                    sha.update(line.encode('utf-8'))
                    n_ok += 1
            offset += len(feats)
            if progress:
                txt = (f'{_n(len(ids))} von {_n(total)} Adressen geladen …' if total
                       else f'{_n(len(ids))} Adressen geladen …')
                progress(len(ids), total, txt)
            if not feats or not new or (total and len(ids) >= total):
                break
            nxt = _ogc_next_link(data, url)
            if nxt:
                url = nxt
            elif data.get('links') and len(feats) < limit and not (total and len(ids) < total):
                url = None                 # links without 'next': this was the last page
            else:
                url = _ogc_page_url(entry, offset)
    if total and len(ids) < total:
        raise SourceError(f'Der Download war unvollständig ({_n(len(ids))} von {_n(total)} Adressen). '
                          'Der bisherige Bestand bleibt erhalten — bitte später erneut versuchen.',
                          detail=f'ogcapi {entry["url"]}: {len(ids)} eindeutige von {total}',
                          kind='voruebergehend')
    digest = sha.hexdigest()
    _detail(f'{bundesland}: {n_ok} Adressen aus {len(ids)} Objekten, SHA-256 {digest}')
    if prev_meta and not force and prev_meta.get('sha256') == digest:
        _unlink(out)
        return _unchanged(entry, fmt='hkde')
    _log(f'{_n(n_ok)} Adressen geladen.')
    meta = _meta(entry, sha256=digest, dateien=os.path.basename(out))
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
        raise SourceError(_SOURCE_CHANGED, kind='geaendert', detail=f'Keine Bestände für {entry["objekt"]} in {base}')
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
        return _unchanged(entry)
    _log_source(_log, entry)
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
    meta = _meta(entry, etag=kennung, last_modified=lm, sha256=info.get('sha256'),
                 dateien=';'.join(os.path.basename(f) for f in files))
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
            return _unchanged(entry)
        _log(f'Neue Daten bei {geaendert} von {len(urls)} Dateien.')
    _log_source(_log, entry)
    if entry.get('dauer'):
        _log(f'Download und Umwandlung dauern {entry["dauer"]}.')
    meta = _meta(entry, dateien=f'{len(urls)} Dateien')
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
        return _unchanged(entry, 'Die Datei ist seit dem letzten Import unverändert.')
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
    meta = _meta(entry, url=path, etag=etag, last_modified=lm,
                 dateien=';'.join(os.path.basename(f) for f in files))
    return {'status': 'downloaded', 'files': files, 'format': entry['format'],
            'entry': entry, 'meta': meta, 'text': '', 'temp_dir': temp_dir}


def test_source(entry, http=None, user_agent_version='dev'):
    """Check a source without importing anything (for the source editor).
    Returns (ok, text) — never raises."""
    try:
        validate_catalog({'sources': {'Test': entry}})
    except Exception as e:
        return False, f'Eintrag ungültig: {e}'
    try:
        return _test_source(entry, http, user_agent_version)
    except SourceError as e:
        return False, str(e)
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _test_source(entry, http, user_agent_version):
    t = SOURCE_TYPES[entry['typ']]
    if t.test is None:
        return False, 'Manuelle Quellen werden nicht abgerufen.'
    if t.automatic:
        http = http or make_http(user_agent(user_agent_version), timeout=30)
    return t.test(entry, http)


def _test_file(entry, _http):
    if not os.path.isfile(entry['pfad']):
        return False, 'Die Datei wurde nicht gefunden.'
    _etag, lm, size = file_signature(entry['pfad'])
    return True, f'Datei vorhanden · {_mb(size)} MB · Stand {format_last_modified(lm)}'


def _test_gaialight(entry, http):
    best = gaialight_latest(entry, http)
    return True, f'Neuester Bestand: {best["bestand"]} vom {_datum_de(best["datum"][:10])}'


def _test_index_geojson(entry, http):
    urls = index_geojson_urls(entry, http)
    return True, f'{len(urls)} Dateien in der Dateiliste, z.B. {_file_name_from_url(urls[0])}'


def _test_wfs(entry, http):
    typname = entry['typnamen'][-1]
    n = wfs_hits(http, entry['url'], typname)
    return True, f'Dienst erreichbar: {_n(n)} {_de(typname)}.'


def _test_ogcapi(entry, http):
    data = _ogc_get_json(http, _ogc_page_url(entry, 0, limit=1))
    feats = data.get('features') if isinstance(data, dict) else None
    if feats is None:
        return False, 'Der Dienst antwortet, liefert aber keine Objekte – Adresse prüfen.'
    total = data.get('numberMatched')
    return True, ('Dienst erreichbar' + (f': {_n(total)} Adressen' if total else '') + '.')


def _test_files(entry, http):
    urls = resolve_download_urls(entry, http)
    status, hdrs, _b = http.get(urls[0], headers={'Range': 'bytes=0-3'}, max_bytes=4)
    if _is_html(hdrs):
        return False, (f'Der Server {_host(urls[0])} liefert derzeit keine Datei, sondern eine '
                       'Webseite (z.B. Wartung).')
    size = hdrs.get('content-length')
    if hdrs.get('content-range', '').rpartition('/')[2].isdigit():
        size = hdrs['content-range'].rpartition('/')[2]
    elif status == 206:
        size = None
    stand = format_last_modified(hdrs.get('last-modified'))
    txt = f'{len(urls)} Datei(en) gefunden: ' + ', '.join(_file_name_from_url(u) for u in urls[:5])
    if size and str(size).isdigit():
        txt += f' · {_mb(int(size))} MB'
    if stand:
        txt += f' · Stand {stand}'
    return True, txt


def format_last_modified(value):
    """'Wed, 25 Mar 2026 08:32:06 GMT' -> '25.03.2026' (for display)."""
    if not value:
        return ''
    try:
        return parsedate_to_datetime(value).strftime('%d.%m.%Y')
    except Exception:
        return value


# ── Registry: functions per source type ──────────────────────────────────────

def _register():
    T = SOURCE_TYPES
    T['datei'].update = lambda c: _update_from_file(c.bundesland, c.entry, c.db_path,
                                                    c.prev_meta, c.force, c.log, c.detail)
    T['datei'].test = _test_file
    T['direct'].urls = _urls_direct
    T['index_json'].urls = _urls_index_json
    for typ in ('page_link', 'atom', 'rss'):
        T[typ].urls = _urls_links
    T['ckan'].urls = _urls_ckan
    for typ in ('direct', 'index_json', 'page_link', 'atom', 'rss', 'ckan'):
        T[typ].test = _test_files
    T['wfs'].check, T['wfs'].update, T['wfs'].test = _check_wfs, _update_wfs, _test_wfs
    T['index_geojson'].update = lambda c: _update_index_geojson(
        c.bundesland, c.entry, c.http, c.prev_meta, c.force, c.log, c.detail)
    T['index_geojson'].test = _test_index_geojson
    T['gaialight'].update = lambda c: _update_gaialight(
        c.bundesland, c.entry, c.http, c.cache, c.prev_meta, c.force, c.progress, c.log,
        c.detail, c.cancel)
    T['gaialight'].test = _test_gaialight
    T['ogcapi'].check = _check_felder
    T['ogcapi'].update = lambda c: _update_ogcapi(
        c.bundesland, c.entry, c.http, c.cache, c.prev_meta, c.force, c.progress, c.log,
        c.detail, c.cancel)
    T['ogcapi'].test = _test_ogcapi


_register()
