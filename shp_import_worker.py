# -*- coding: utf-8 -*-
"""
Background worker for importing address shapefiles into the local SQLite
geocoding DB. Supports two geometry types:

- Point shapefiles (e.g. ALKIS AX_GeoreferenzierteGebaeudeadresse.shp):
  the coordinate is read directly from the point geometry.
- Polygon shapefiles (building outlines/Hausumringe, e.g. ALKIS building
  outlines): a representative point on the building footprint
  (pointOnSurface) is used as the coordinate.

The user maps the attribute fields (street, house number, house number
suffix, PLZ, city) via drop-down menus. The Bundesland is chosen via the
Bundesland combo box in the Admin tab. Features without an assigned address
(empty street field) are skipped during import.

This worker:
1. Opens the shapefile via QgsVectorLayer (PyQGIS).
2. Reads the fields mapped by the user for each feature.
3. Determines a representative point of the geometry (points directly,
   polygon/line via pointOnSurface) and transforms it to UTM
   (EPSG:25832/25833) if the source CRS differs.
"""

import os
import sqlite3

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import (
    QgsVectorLayer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsProject, QgsWkbTypes,
)

from . import local_geocoder as _lc
from .local_geocoder import init_db, normalize, _core_gemeinde
from .db_import_worker import _canonical_bundesland, _clean_gemeinde, _is_plausible_hausnummer
from .plz_resolver import build_resolver_logged


BATCH_SIZE = 5000


def _n(x):
    """Integer with German thousands separator: 4509537 -> '4.509.537'."""
    try:
        return f'{int(x):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(x)


def _representative_point(geom):
    """Returns a representative point (x, y) for a geometry:
    - point geometry: the coordinate directly
    - lines/polygon (e.g. building outlines): pointOnSurface() — a point that
      is guaranteed to lie ON/IN the geometry (better than centroid(), which
      can fall outside for concave or L-shaped areas)
    Returns None if the geometry is empty/invalid.
    """
    if geom is None or geom.isEmpty():
        return None

    geom_type = QgsWkbTypes.geometryType(geom.wkbType())

    if geom_type == QgsWkbTypes.PointGeometry:
        # Multi-point possible → asPoint() fails, then take a vertex
        try:
            pt = geom.asPoint()
            return (pt.x(), pt.y())
        except Exception:
            pass

    # Polygon/line (building outlines) or fallback: point on surface
    try:
        pos = geom.pointOnSurface()
        if pos is not None and not pos.isEmpty():
            pt = pos.asPoint()
            return (pt.x(), pt.y())
    except Exception:
        pass

    # Last fallback: centroid
    try:
        c = geom.centroid()
        if c is not None and not c.isEmpty():
            pt = c.asPoint()
            return (pt.x(), pt.y())
    except Exception:
        pass

    return None


def _layer_epsg(layer):
    """Returns the EPSG code of the layer CRS as an integer, or None
    if it cannot be determined."""
    crs = layer.crs()
    if not crs.isValid():
        return None
    auth_id = crs.authid()  # e.g. 'EPSG:25832'
    if auth_id.startswith('EPSG:'):
        try:
            return int(auth_id.split(':')[1])
        except ValueError:
            return None
    return None


def get_layer_fields(filepath):
    """Opens the shapefile and returns the list of attribute field names,
    or None if the layer cannot be loaded."""
    try:
        layer = QgsVectorLayer(filepath, 'shp_fields', 'ogr')
        if not layer.isValid():
            return None
        return [f.name() for f in layer.fields()]
    except Exception:
        return None


def get_layer_fields_with_error(filepath):
    """Like get_layer_fields, but returns (fields, error_msg).
    Provides a detailed diagnosis if the layer cannot be loaded."""
    import os
    # Preliminary checks for meaningful error messages
    if not os.path.exists(filepath):
        return None, f'Datei nicht gefunden: {filepath}'
    base = os.path.splitext(filepath)[0]
    missing = [ext for ext in ('.shx', '.dbf') if not os.path.exists(base + ext)]
    if missing:
        return None, ('Fehlende Begleitdateien: ' + ', '.join(missing) +
                       ' — ein Shapefile benötigt .shp, .shx und .dbf im selben Ordner.')
    try:
        layer = QgsVectorLayer(filepath, 'shp_fields', 'ogr')
        if not layer.isValid():
            err = ''
            try:
                err = layer.error().message()
            except Exception:
                pass
            if not err:
                err = ('OGR konnte die Datei nicht öffnen. Mögliche Ursachen: '
                       'Datei durch anderes Programm gesperrt, beschädigt, '
                       'oder ungültige Geometrie.')
            return None, err
        n = layer.featureCount()
        flds = [f.name() for f in layer.fields()]
        if not flds:
            return None, 'Das Shapefile enthält keine Attributfelder.'
        return flds, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


class ShpImportWorker(QObject):

    progress  = pyqtSignal(int, int, str)   # read so far, total, status
    log       = pyqtSignal(str)             # log for the user
    detail    = pyqtSignal(str)             # technical details (QGIS message log)
    finished  = pyqtSignal(bool, str)

    def __init__(self, filepath, bundesland, field_map, replace=True,
                 rebuild_lookup=True, announce=True):
        """
        filepath:   path to the .shp file
        bundesland: Bundesland (required — chosen via the Bundesland combo box
                     in the Admin tab, since shapefiles usually have no
                     Bundesland attribute)
        field_map:  dict with the field names mapped by the user:
                     {
                         'strasse':       <field name, required>,
                         'hausnummer':    <field name, required>,
                         'hausnr_zusatz': <field name or None>,
                         'plz':           <field name or None>,
                         'gemeinde':      <field name or None>,
                     }
                     If 'gemeinde' is not mapped, the Bundesland name is
                     used as the municipality (as in the Berlin import).
        replace:    replace existing data of this Bundesland
        rebuild_lookup: rebuild the search index at the end (False for the
                     batch download — done once at the end there)
        announce:   name the file in the log (False: message log only)
        """
        super().__init__()
        self.filepath   = filepath
        self.bundesland = _canonical_bundesland(bundesland)
        self.field_map  = field_map
        self.replace    = replace
        self.rebuild_lookup = rebuild_lookup
        self.announce   = announce
        self._cancelled = False
        self._plz_source    = None
        self._plz_field     = 'plz'
        self._plz_layername = None

    def set_plz_layer(self, source, field='plz', layername=None):
        """Optional PLZ area layer; missing PLZ are filled in during import."""
        self._plz_source    = source
        self._plz_field     = field or 'plz'
        self._plz_layername = layername

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            import traceback
            self.detail.emit(traceback.format_exc())
            self.finished.emit(False, f'Import fehlgeschlagen: {e}. '
                                      'Der bisherige Datenbestand bleibt erhalten.')

    def _run(self):
        (self.log if self.announce else self.detail).emit(
            f'Lese {os.path.basename(self.filepath)} …')
        self.detail.emit(f'Shapefile: {self.filepath}')

        layer = QgsVectorLayer(self.filepath, 'shp_import', 'ogr')
        if not layer.isValid():
            self.finished.emit(False, 'Das Shapefile konnte nicht geöffnet werden.')
            return

        total = layer.featureCount()
        self.log.emit(f'{_n(total)} Objekte im Shapefile.')

        f_strasse  = self.field_map.get('strasse')
        f_hausnr   = self.field_map.get('hausnummer')
        f_zusatz   = self.field_map.get('hausnr_zusatz')
        f_plz      = self.field_map.get('plz')
        f_gemeinde = self.field_map.get('gemeinde')

        if not f_strasse or not f_hausnr:
            self.finished.emit(False, 'Straße und Hausnummer müssen '
                                       'zugeordnet werden.')
            return

        # ── Prepare the coordinate transformation ────────────────────────
        src_epsg = _layer_epsg(layer)
        if src_epsg is None:
            self.finished.emit(False, 'Das Koordinatensystem des Shapefiles ist nicht '
                                       'bekannt (.prj-Datei fehlt?).')
            return

        if src_epsg in (25832, 25833):
            target_epsg = src_epsg
            transform = None
        else:
            target_epsg = 25832
            transform = QgsCoordinateTransform(
                layer.crs(),
                QgsCoordinateReferenceSystem(f'EPSG:{target_epsg}'),
                QgsProject.instance()
            )
            self.detail.emit(f'Transformiere Koordinaten von EPSG:{src_epsg} '
                             f'nach EPSG:{target_epsg}.')

        conn = sqlite3.connect(_lc.DB_PATH)
        init_db(conn)

        try:

            # DELETE and all INSERTs in ONE transaction (commit only at the end,
            # rollback on error/cancel) — as in the HK-DE import. Previously the
            # DELETE was committed immediately; a cancel left the old data deleted
            # and the partial data in place.
            if self.replace:
                conn.execute("DELETE FROM addresses WHERE bundesland = ?",
                              (self.bundesland,))
                self.log.emit(f'Die bisherigen Adressen für {self.bundesland} werden ersetzt.')

            n_ok = 0
            n_skip = 0
            n_features = 0
            batch = []

            resolver = build_resolver_logged(self._plz_source, self._plz_field,
                                             self._plz_layername, self.log.emit,
                                             self.detail.emit)

            for feature in layer.getFeatures():
                if self._cancelled:
                    conn.rollback()   # discard the deletion + inserts so far
                    conn.close()
                    self.finished.emit(False, 'Import abgebrochen – der bisherige '
                                              'Datenbestand bleibt erhalten.')
                    return

                n_features += 1
                if n_features % 5000 == 0:
                    self.progress.emit(n_features, total, f'{_n(n_ok)} Adressen eingelesen …')

                geom = feature.geometry()
                rp = _representative_point(geom)
                if rp is None:
                    n_skip += 1
                    continue
                x, y = rp
                if transform is not None:
                    try:
                        from qgis.core import QgsPointXY
                        pt = transform.transform(QgsPointXY(x, y))
                        x, y = pt.x(), pt.y()
                    except Exception:
                        n_skip += 1
                        continue

                def _attr(fname):
                    if not fname:
                        return ''
                    val = feature[fname]
                    return '' if val is None else str(val).strip()

                strasse = _attr(f_strasse)
                if not strasse:
                    # building outline without an assigned address → skip
                    n_skip += 1
                    continue

                hnr_raw = _attr(f_hausnr)
                if hnr_raw and not _is_plausible_hausnummer(hnr_raw):
                    n_skip += 1
                    continue

                gemeinde_val = _clean_gemeinde(_attr(f_gemeinde)) or self.bundesland

                plz_val = _attr(f_plz)
                herkunft = None
                if not plz_val and resolver is not None:
                    plz_val = resolver.lookup(x, y, target_epsg)
                    herkunft = 'gebiet' if plz_val else None

                batch.append((
                    self.bundesland,
                    '',                          # kreis (no standard field mapped)
                    gemeinde_val,
                    plz_val,
                    strasse,
                    hnr_raw,
                    _attr(f_zusatz),
                    target_epsg,
                    x,
                    y,
                    normalize(strasse),
                    _core_gemeinde(gemeinde_val),
                    herkunft,
                ))
                n_ok += 1

                if len(batch) >= BATCH_SIZE:
                    self._flush(conn, batch)
                    batch = []

            if batch:
                self._flush(conn, batch)

            if n_ok == 0:
                # Empty or wrong file (e.g. maintenance page instead of data): replace
                # nothing — otherwise the state's existing data would be deleted
                conn.rollback()
                conn.close()
                self.finished.emit(False, 'In der Datei wurden keine Adressen gefunden – der bisherige '
                                          'Datenbestand bleibt erhalten.')
                return
            _lc.record_plz_meta(conn, self.bundesland,
                                _lc.resolver_info(resolver, self._plz_source,
                                                  self._plz_field, self._plz_layername),
                                self.replace)
            # Municipality/locality index for the new data (part of the same
            # transaction), then ONE atomic commit including the DELETE.
            if self.rebuild_lookup:
                _lc.rebuild_lookup_tables(conn, self.log.emit)
            conn.commit()
            if resolver is not None:
                st = resolver.stats()
                if st["matched"] or st["missed"]:   # nothing to fill in → no message
                    self.log.emit(f"PLZ ergänzt für {_n(st['matched'])} Adressen"
                                  + (f"; {_n(st['missed'])} liegen außerhalb der PLZ-Flächen."
                                     if st['missed'] else '.'))
            conn.close()

        except Exception:
            conn.rollback()     # discard DELETE + inserts so far completely
            conn.close()
            raise               # run() reports the error; existing data is kept

        self.progress.emit(n_features, total, 'Fertig.')
        msg = f'Import abgeschlossen: {_n(n_ok)} Adressen importiert'
        if n_skip:
            msg += f' ({_n(n_skip)} Objekte ohne verwertbare Adresse übersprungen)'
        self.finished.emit(True, msg + '.')

    def _flush(self, conn, batch):
        conn.executemany("""
            INSERT INTO addresses
              (bundesland, kreis, gemeinde, plz, strasse, hausnummer,
               hausnr_zusatz, epsg, x, y, strasse_norm, gemeinde_norm, plz_herkunft)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, batch)
        # NO commit per batch — the whole transaction is committed in _run.

    @staticmethod
    def preview_file(filepath, field_map=None, n_features=2):
        """Loads the shapefile and returns a preview:
        {
            'available_fields': [field names of the attribute table],
            'epsg': int or None,
            'feature_count': int,
            'previews': [ {strasse, hausnummer, hausnr_zusatz, plz,
                            gemeinde, x, y}, ... ]
        }
        previews uses the mapping passed in field_map
        (e.g. for a live preview while the user maps fields).
        Returns None if the layer cannot be loaded.
        """
        layer = QgsVectorLayer(filepath, 'shp_preview', 'ogr')
        if not layer.isValid():
            return None

        field_map = field_map or {}
        src_epsg = _layer_epsg(layer)

        # Determine the geometry type for display
        gtype = QgsWkbTypes.geometryType(layer.wkbType())
        geom_label = {
            QgsWkbTypes.PointGeometry:   'Punkt (Adresspunkte)',
            QgsWkbTypes.PolygonGeometry: 'Polygon (Hausumringe — Flächenmittelpunkt wird verwendet)',
            QgsWkbTypes.LineGeometry:    'Linie',
        }.get(gtype, 'Unbekannt')

        previews = []
        for feature in layer.getFeatures():
            geom = feature.geometry()
            rp = _representative_point(geom)
            x, y = (rp if rp is not None else (None, None))

            def _attr(logical):
                fname = field_map.get(logical)
                if not fname:
                    return ''
                val = feature[fname]
                return '' if val is None else str(val).strip()

            previews.append({
                'strasse':       _attr('strasse'),
                'hausnummer':    _attr('hausnummer'),
                'hausnr_zusatz': _attr('hausnr_zusatz'),
                'plz':           _attr('plz'),
                'gemeinde':      _attr('gemeinde'),
                'x': x,
                'y': y,
            })
            if len(previews) >= n_features:
                break

        return {
            'available_fields': [f.name() for f in layer.fields()],
            'epsg': src_epsg,
            'geometry_type': geom_label,
            'feature_count': layer.featureCount(),
            'previews': previews,
        }
