# -*- coding: utf-8 -*-
"""
Background worker "download and import sources".

For one or more Bundesländer (default: all with a registered source —
internet or a file on this computer):
  1. fetch the source via source_downloader — conditionally, i.e. only if
     there is new data since the last import (ETag/Last-Modified, checksum,
     size/modification time for files); sources with a lock after download
     (min_abstand_tage) are skipped as long as the last download is more recent;
  2. load the data files with the existing import workers (HK-DE text,
     INSPIRE GML, shapefile) — all files of a Bundesland in ONE transaction
     that replaces its data (on any error or cancel nothing changes);
  3. record the state in source_meta;
  4. at the end, rebuild the search index (municipality/locality index) ONCE
     (instead of after each individual state).

An error in one state does not abort the others; the summary reports per
state: updated / no new data / manual only / error.

Signals: progress(int, int, str), log(str) for the log window (texts for
end users), detail(str) for technical details (QGIS message log),
finished(bool, str).
"""

import os
import json
import shutil
import sqlite3
import urllib.parse
from email.utils import parsedate_to_datetime

from qgis.PyQt.QtCore import QObject, pyqtSignal

from . import local_geocoder as _lc
from . import source_downloader as _sd
from .source_downloader import Cancelled, SourceError, format_last_modified


# Internal status codes → display text
STATUS_TEXT = {
    'aktualisiert': 'aktualisiert',
    'unverändert':  'keine neuen Daten',
    'übersprungen': 'übersprungen',
    'manuell':      'keine Quelle eingetragen',
    'fehler':       'Fehler',
    'abgebrochen':  'abgebrochen',
}


def _n(x):
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


def _http_date(value):
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        return 0


def _date(iso):
    if not iso:
        return ''
    try:
        y, m, d = iso[:10].split('-')
        return f'{d}.{m}.{y}'
    except Exception:
        return iso[:10]


class SourceUpdateWorker(QObject):

    progress = pyqtSignal(int, int, str)
    log      = pyqtSignal(str)
    detail   = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, bundeslaender=None, force=False, replace=True, plugin_version='dev',
                 check_catalog=False):
        """bundeslaender: name, list of names or None (= all registered
        sources in the catalog)."""
        super().__init__()
        if isinstance(bundeslaender, str):
            bundeslaender = [bundeslaender]
        self.bundeslaender  = list(bundeslaender) if bundeslaender else None
        self.single         = bool(self.bundeslaender) and len(self.bundeslaender) == 1
        self.force          = force
        self.replace        = replace
        self.plugin_version = plugin_version
        self.check_catalog  = check_catalog   # check the online source list first
        self.catalog_status = None
        self.summary        = []       # [(bundesland, status, text)]
        self.plz_ergaenzt   = {}       # {bundesland: count} from the postcode (PLZ) post-pass
        self._cancelled     = False
        self._current       = None     # currently running import worker
        self._prefix        = ''

    def cancel(self):
        self._cancelled = True
        if self._current is not None:
            try:
                self._current.cancel()
            except Exception:
                pass

    def _is_cancelled(self):
        return self._cancelled

    def _emit_progress(self, done, total, text):
        done, total = int(done), int(total)
        while max(done, total) > 2_000_000_000:      # Qt int signal: stay below 2^31
            done, total = done // 1024, total // 1024
        self.progress.emit(done, total, f'{self._prefix}{text}')

    # ------------------------------------------------------------------ #
    def run(self):
        try:
            self._run()
        except Exception as e:
            import traceback
            self.detail.emit(traceback.format_exc())
            self.finished.emit(False, f'Unerwarteter Fehler: {e}')

    def _run(self):
        if self.check_catalog:
            try:
                self.catalog_status, text = _sd.fetch_online_catalog(
                    user_agent_version=self.plugin_version)
            except Exception as e:      # never let the list check stop the update
                self.catalog_status, text = 'fehler', f'Quellenliste nicht geprüft ({e})'
            if self.catalog_status in ('neu', 'zu_neu'):
                self.log.emit(text)
            else:
                self.detail.emit(f'Quellenliste: {text}')
        cat     = _sd.load_catalog()
        targets = self.bundeslaender or _sd.registered_bundeslaender(cat)
        updated = []                   # states with new data → renew their directory entries

        for i, bl in enumerate(targets):
            if self._cancelled:
                self.summary.append((bl, 'abgebrochen', ''))
                continue
            self._prefix = '' if self.single else f'{bl} ({i + 1} von {len(targets)}): '
            if not self.single:
                self.log.emit(f'\n{bl} ({i + 1} von {len(targets)})')
            try:
                status, text = self._update_one(bl, cat)
            except Cancelled:
                status, text = 'abgebrochen', ('Abgebrochen – der bisherige Datenbestand bleibt '
                                               'erhalten.')
                self._cancelled = True
            except SourceError as e:
                status, text = 'fehler', str(e)
                if e.detail:
                    self.detail.emit(f'{bl}: {e.detail}')
            except Exception as e:
                import traceback
                self.detail.emit(f'{bl}: {traceback.format_exc()}')
                status, text = 'fehler', f'Unerwarteter Fehler: {e}'
            if status == 'aktualisiert':
                updated.append(bl)
            self.summary.append((bl, status, text))
            if not self.single and status in ('fehler', 'abgebrochen'):
                self.log.emit(f'{STATUS_TEXT[status]}: {text}')

        # Postcode (PLZ) post-pass: fill in missing postcodes from the bundled
        # postcode areas — also in states without new data (e.g. after an import
        # with an older plugin version or after new postcode areas).
        if not self._cancelled:
            self._prefix = ''
            try:
                from .plz_resolver import ergaenze_plz, active_source
                if active_source():
                    self.plz_ergaenzt = ergaenze_plz(
                        _lc.DB_PATH, log=self.log.emit, detail=self.detail.emit,
                        progress=self._emit_progress, cancel=self._is_cancelled)
                else:
                    self.detail.emit('PLZ-Nachlauf übersprungen: keine PLZ-Gebiete verfügbar.')
            except Exception:
                import traceback
                self.detail.emit('PLZ-Nachlauf fehlgeschlagen:\n' + traceback.format_exc())
                self.log.emit('Die PLZ-Ergänzung konnte nicht durchgeführt werden.')

        # Directory (places, districts, PLZ coverage) only for the states that
        # changed: new data or postcodes filled in by the post-pass
        changed = updated + [bl for bl, n in self.plz_ergaenzt.items() if n and bl not in updated]
        if changed:
            self._prefix = ''
            self._emit_progress(0, 0, 'Suchverzeichnis wird aktualisiert …')
            conn = sqlite3.connect(_lc.DB_PATH)
            try:
                _lc.rebuild_lookup_tables(conn, self.log.emit, cancel=self._is_cancelled,
                                          bundeslaender=changed)
                conn.commit()
            except _lc.RebuildCancelled:
                # The previous directory stays; it is rebuilt automatically at the
                # next geocoding run because the address count no longer matches.
                self.log.emit('Abgebrochen – das Suchverzeichnis wird beim nächsten Geocodieren '
                              'aktualisiert.')
            finally:
                conn.close()

        self._emit_progress(1, 1, 'Fertig.')
        self.finished.emit(*self._result())

    def _plz_lines(self):
        done = {bl: n for bl, n in self.plz_ergaenzt.items() if n}
        if not done:
            return []
        return ['PLZ ergänzt: ' + ', '.join(f'{bl} ({_n(n)} Adressen)' for bl, n in done.items()) + '.']

    def _result(self):
        if self.single:
            bl, status, text = self.summary[0]
            ok = status in ('aktualisiert', 'unverändert', 'übersprungen')
            msg = f'{bl}: {text}' if status == 'aktualisiert' else text
            return ok, '\n'.join([msg] + self._plz_lines())
        counts = {}
        for _bl, st, _t in self.summary:
            counts[st] = counts.get(st, 0) + 1
        head = ', '.join(f'{n}× {STATUS_TEXT[st]}' for st, n in counts.items())
        lines = [f'Quellen-Aktualisierung abgeschlossen: {head}.']
        for bl, st, text in self.summary:
            if st == 'aktualisiert':
                lines.append(f'  • {bl}: {text}')
            else:
                lines.append(f'  • {bl}: {STATUS_TEXT[st]}' + (f' – {text}' if text else ''))
        lines += self._plz_lines()
        ok = not any(st in ('fehler', 'abgebrochen') for _bl, st, _t in self.summary)
        return ok, '\n'.join(lines)

    # ------------------------------------------------------------------ #
    def _update_one(self, bl, cat):
        """Fetch one Bundesland and import it if needed; if the main source fails
        and there is a fallback source ('ausweich'), that one is tried.
        Returns (status, text) with status aktualisiert|unverändert|übersprungen|manuell."""
        entry = cat['sources'].get(bl)
        if not _sd.is_registered(entry):
            if self.single:
                raise SourceError(_sd.manual_text(bl, entry))
            return 'manuell', ''
        try:
            return self._update_entry(bl, cat)
        except SourceError as e:
            alt = entry.get('ausweich')
            if not alt or self._cancelled:
                raise
            if alt.get('nur_ohne_bestand') and self._has_addresses(bl):
                # Fallback source is older/less complete than the existing data
                raise SourceError(f'{e} Die Ausweichquelle wird nur genutzt, solange für {bl} noch '
                                  'keine Adressen vorhanden sind.', detail=e.detail) from e
            if e.detail:
                self.detail.emit(f'{bl}: Hauptquelle: {e.detail}')
            self.log.emit(f'Hauptquelle nicht nutzbar ({e}) – verwende die Ausweichquelle.')
            cat2 = dict(cat)
            cat2['sources'] = dict(cat['sources'])
            cat2['sources'][bl] = dict(alt, _herkunft=entry.get('_herkunft', 'standard'))
            return self._update_entry(bl, cat2)

    def _has_addresses(self, bl):
        """Does the state already have addresses? If the database cannot be
        read (e.g. locked), assume yes — so that an older fallback source never
        replaces existing data by mistake."""
        try:
            conn = sqlite3.connect(_lc.DB_PATH, timeout=30)
            try:
                return conn.execute('SELECT 1 FROM addresses WHERE bundesland = ? LIMIT 1',
                                    (bl,)).fetchone() is not None
            finally:
                conn.close()
        except Exception as e:
            self.detail.emit(f'{bl}: Bestand nicht lesbar ({e}) – Ausweichquelle nicht genutzt')
            return True

    def _update_entry(self, bl, cat):
        prev = _lc.get_source_meta(bl)
        if prev and not self.force:
            stand = format_last_modified(prev.get('last_modified'))
            self.log.emit(f'Letzter Import: {_date(prev.get("importiert_am")) or "unbekannt"}'
                          + (f' (Datenstand {stand})' if stand else ''))

        result = _sd.update_source(bl, _lc.DB_PATH, prev_meta=prev, force=self.force,
                                   progress=self._emit_progress, log=self.log.emit,
                                   detail=self.detail.emit,
                                   cancel=self._is_cancelled, catalog=cat,
                                   user_agent_version=self.plugin_version)
        if result['status'] == 'manual':
            return 'manuell', result['text']
        if result['status'] == 'skipped':
            if not self.single:
                self.log.emit(result['text'])
            return 'übersprungen', result['text']
        if result['status'] == 'unchanged':
            if not self.single:          # for a single fetch the text is in the final message
                self.log.emit(result['text'])
            return 'unverändert', result['text']

        if self._cancelled:
            raise Cancelled()
        files = result['files']
        fmt   = result['format']
        self._remote_meta = {}
        if result.get('remote'):
            ok, msg = self._import_remote(bl, result['remote'], fmt, result.get('entry') or {})
        else:
            self.detail.emit(f'{bl}: Import ({fmt}) von {len(files)} Datei(en)')
            self.log.emit('Importiere Adressen …')
            ok, msg = self._import(bl, files, fmt, result.get('entry') or {})
        if not ok:
            if self._cancelled:
                raise Cancelled()
            raise SourceError(msg)

        meta = dict(result['meta'])
        meta.update(self._remote_meta)
        meta['importiert_am'] = meta.get('heruntergeladen_am')
        try:
            meta['adressen'] = _lc.count_addresses(bl)
        except Exception:
            meta['adressen'] = None
        _lc.save_source_meta(bl, **meta)
        if _sd.is_file_source(result.get('entry')):
            # Delete only the extracted copies — never the user's file
            if result.get('temp_dir'):
                shutil.rmtree(result['temp_dir'], ignore_errors=True)
        else:
            self._cleanup_cache(bl)
        n = meta.get('adressen')
        return 'aktualisiert', (f'{_n(n)} Adressen importiert.' if n else msg)

    def _cleanup_cache(self, bl):
        """Delete downloaded files after a successful import. For the next
        fetch the state in source_meta (ETag/checksum) is sufficient — the
        files themselves would only take up space (Hamburg WFS: about 1.6 GB GML).
        On error they are kept for diagnosis and overwritten on the
        next fetch."""
        cache = _sd.cache_dir_for(_lc.DB_PATH, bl)
        if not os.path.isdir(cache):
            return
        size = 0
        for root, _dirs, files in os.walk(cache):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        shutil.rmtree(cache, ignore_errors=True)
        self.detail.emit(f'{bl}: Temporäre Download-Dateien gelöscht ({_n(size / 1e6)} MB, {cache}).')

    def _import(self, bl, files, fmt, entry=None):
        """Load data files with the matching import worker — all files of the
        Bundesland in ONE transaction that replaces its data (if requested): on
        an error, an empty file or a cancel nothing changes. The index is only
        built at the end of _run. Returns (ok, message)."""
        entry = entry or {}
        if self._cancelled:
            return False, 'Abgebrochen.'
        if fmt == 'osm':
            return self._import_osm(bl, files, entry)
        # Without "Auch ohne neue Daten erneut laden" a much smaller dataset
        # (e.g. an incomplete file) does not replace the existing one.
        common = dict(replace=self.replace, rebuild_lookup=False, announce=False,
                      allow_shrink=self.force)
        if fmt == 'gml':
            from .gml_import_worker import GmlImportWorker
            w = GmlImportWorker(files, bl, gemeinde_level=entry.get('gemeinde_ebene'), **common)
        elif fmt == 'hkde':
            from .db_import_worker import DbImportWorker
            w = DbImportWorker(list(files), bl, **common)
        elif fmt == 'shp':
            from .shp_import_worker import ShpImportWorker
            # A zip yields .shp, .shx, .dbf, .prj … — the import reads the .shp files
            files = [f for f in files if f.lower().endswith('.shp')]
            if not files:
                return False, 'Im Download wurde kein Shapefile gefunden.'
            w = ShpImportWorker(files, bl, entry.get('felder') or {}, **common)
        else:
            return False, f'Format {fmt!r} wird vom automatischen Import nicht unterstützt.'
        outcome = {}
        self._wire(w, lambda ok, msg: outcome.update(ok=ok, msg=msg))
        w.run()
        return outcome.get('ok', False), outcome.get('msg', 'Import fehlgeschlagen')

    def _import_remote(self, bl, urls, fmt, entry):
        """Many files (one per district/Kreis): download each file, convert it to
        address rows and delete it immediately; at the end load all rows in one import."""
        if fmt != 'alkis':
            return False, f'Format {fmt!r} wird für Dateilisten nicht unterstützt.'
        from .osm_convert import gdal_available
        from .alkis_convert import alkis_to_rows, write_header, kreis_aus_dateiname, AlkisConvertError
        if not gdal_available():
            return False, 'Diese QGIS-Installation kann keine GeoPackages lesen (GDAL fehlt).'
        cache = _sd.cache_dir_for(_lc.DB_PATH, bl)
        os.makedirs(cache, exist_ok=True)
        txt = os.path.join(cache, 'adressen_alkis.txt')
        http = _sd.Http(_sd.user_agent(self.plugin_version), timeout=120)
        seen, etags, stand = set(), {}, []
        summe = {'adressen': 0, 'doppelt': 0, 'ohne_gemeinde': 0}
        try:
            with open(txt, 'w', encoding='utf-8', newline='') as fh:
                write_header(fh)
                for i, url in enumerate(urls):
                    if self._cancelled:
                        raise Cancelled()
                    kreis = kreis_aus_dateiname(urllib.parse.unquote(url))
                    self._prefix = f'{kreis} ({i + 1} von {len(urls)}): '

                    def _p(done, total, _k=kreis):
                        self._emit_progress(done, total, f'Lade Daten: {_n(done / 1e6)} MB'
                                            + (f' von {_n(total / 1e6)} MB' if total else '') + ' …')
                    info = _sd.download_file(http, url, cache, force=True, progress=_p,
                                             cancel=self._is_cancelled)
                    etags[url] = info.get('etag') or ''
                    if info.get('last_modified'):
                        stand.append(info['last_modified'])
                    try:
                        st = alkis_to_rows(info['path'], fh, bl, kreis, entry.get('alkis'),
                                           cancel=self._is_cancelled, seen=seen,
                                           progress=self._emit_progress, detail=self.detail.emit)
                    finally:
                        try:
                            os.remove(info['path'])
                        except OSError:
                            pass
                    for k in summe:
                        summe[k] += st.get(k, 0)
                    self.log.emit(f'{kreis}: {_n(st["adressen"])} Adressen')
            self._prefix = ''
            self.log.emit(f'{_n(summe["adressen"])} Adressen aus {len(urls)} Dateien übernommen'
                          + (f' ({_n(summe["doppelt"])} doppelte zusammengeführt)' if summe['doppelt'] else '')
                          + '.')
            if summe['ohne_gemeinde']:
                self.detail.emit(f'{bl}: {summe["ohne_gemeinde"]} Gebäude ohne Gemeinde')
            self._remote_meta = {'etag': json.dumps(etags, sort_keys=True),
                                 'last_modified': max(stand, key=_http_date) if stand else None,
                                 'dateien': f'{len(urls)} Dateien'}
            self.log.emit('Importiere Adressen …')
            return self._import(bl, [txt], 'hkde', entry)
        except AlkisConvertError as e:
            return False, str(e)
        finally:
            try:
                os.remove(txt)
            except OSError:
                pass

    def _import_osm(self, bl, files, entry):
        """Convert OpenStreetMap extract(s) into an HK-BY file and load it with
        the HK-DE import. The intermediate files are stored in
        <Cache>/_osm and deleted afterwards."""
        from .osm_convert import osm_to_hkby, gdal_available, OsmConvertError
        if not gdal_available():
            return False, ('Diese QGIS-Installation kann keine OpenStreetMap-Dateien lesen '
                           '(GDAL ohne OSM-Treiber).')
        work = os.path.join(_sd.cache_dir_for(_lc.DB_PATH, bl), '_osm')
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        try:
            txts = []
            for i, f in enumerate(files):
                self.log.emit('Wandle OpenStreetMap-Daten in Adressen um …')
                out = os.path.join(work, f'adressen_{i + 1}.txt')
                st = osm_to_hkby(f, out, bl, progress=self._emit_progress,
                                 cancel=self._is_cancelled, detail=self.detail.emit)
                extra = []
                if st['doppelt']:
                    extra.append(f'{_n(st["doppelt"])} doppelte zusammengeführt')
                if st['gemeinde_ergaenzt']:
                    extra.append(f'Gemeinde für {_n(st["gemeinde_ergaenzt"])} Adressen aus den '
                                 'Gemeindegrenzen ergänzt')
                self.log.emit(f'{_n(st["adressen"])} Adressen aus OpenStreetMap übernommen'
                              + (f' ({"; ".join(extra)})' if extra else '') + '.')
                txts.append(out)
            return self._import(bl, txts, 'hkde', entry)
        except OsmConvertError as e:
            return False, str(e)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _wire(self, worker, done_cb):
        self._current = worker
        if self._cancelled:              # cancel arrived while the worker was being set up
            worker.cancel()
        worker.log.connect(self.log.emit)
        if hasattr(worker, 'detail'):
            worker.detail.connect(self.detail.emit)
        worker.progress.connect(self._emit_progress)
        worker.finished.connect(done_cb)
