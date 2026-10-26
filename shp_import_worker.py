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
1. Opens the shapefile(s) via QgsVectorLayer (PyQGIS).
2. Reads the fields mapped by the user for each feature.
3. Determines a representative point of the geometry (points directly,
   polygon/line via pointOnSurface) and transforms it to UTM
   (EPSG:25832/25833) if the source CRS differs.
All files of one worker are imported in ONE transaction (AddressSink).
"""

import contextlib
import os

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import (
    QgsVectorLayer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsProject, QgsWkbTypes, QgsPointXY,
)

from .db_import_worker import (
    _canonical_bundesland, _clean_gemeinde, _hausnummer_rows, _as_list,
    AddressSink, CANCEL_MSG, KEEP_MSG, no_addresses_msg,
)
from .plz_resolver import build_resolver_logged, attr_text, plz_text


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
        with contextlib.suppress(Exception):
            pt = geom.asPoint()
            return (pt.x(), pt.y())

    # Polygon/line (building outlines) or fallback: point on surface
    with contextlib.suppress(Exception):
        pos = geom.pointOnSurface()
        if pos is not None and not pos.isEmpty():
            pt = pos.asPoint()
            return (pt.x(), pt.y())

    # Last fallback: centroid
    with contextlib.suppress(Exception):
        c = geom.centroid()
        if c is not None and not c.isEmpty():
            pt = c.asPoint()
            return (pt.x(), pt.y())

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


def get_layer_fields_with_error(filepath):
    """Opens the shapefile and returns (fields, error_msg).
    Provides a detailed diagnosis if the layer cannot be loaded."""
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
            with contextlib.suppress(Exception):
                err = layer.error().message()
            if not err:
                err = ('OGR konnte die Datei nicht öffnen. Mögliche Ursachen: '
                       'Datei durch anderes Programm gesperrt, beschädigt, '
                       'oder ungültige Geometrie.')
            return None, err
        flds = [f.name() for f in layer.fields()]
        if not flds:
            return None, 'Das Shapefile enthält keine Attributfelder.'
        return flds, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


class _ShpError(Exception):
    """Message for the user; the import is rolled back."""


class ShpImportWorker(QObject):

    progress  = pyqtSignal(int, int, str)   # read so far, total, status
    log       = pyqtSignal(str)             # log for the user
    detail    = pyqtSignal(str)             # technical details (QGIS message log)
    finished  = pyqtSignal(bool, str)

    def __init__(self, filepaths, bundesland, felder=None, replace=True,
                 rebuild_lookup=True, announce=True, allow_shrink=False, field_map=None):
        """
        filepaths:  path to the .shp file, or a LIST of .shp files — all are
                     imported in ONE transaction
        bundesland: Bundesland (required — chosen via the Bundesland combo box
                     in the Admin tab, since shapefiles usually have no
                     Bundesland attribute)
        felder:     dict with the field names mapped by the user:
                     {
                         'strasse':       <field name, required>,
                         'hausnummer':    <field name, required>,
                         'hausnr_zusatz': <field name or None>,
                         'plz':           <field name or None>,
                         'gemeinde':      <field name or None>,
                     }
                     If 'gemeinde' is not mapped, the Bundesland name is
                     used as the municipality (as in the Berlin import).
                     (field_map: older keyword name of the same parameter)
        replace:    replace existing data of this Bundesland
        rebuild_lookup: rebuild the search index at the end (False for the
                     batch download — done once at the end there)
        announce:   name the file in the log (False: message log only)
        allow_shrink: accept new data with far fewer addresses than before
        """
        super().__init__()
        self.filepaths  = _as_list(filepaths)
        self.filepath   = self.filepaths[0] if self.filepaths else ''
        self.bundesland = _canonical_bundesland(bundesland)
        self.field_map  = dict(felder if felder is not None else (field_map or {}))
        self.replace    = replace
        self.rebuild_lookup = rebuild_lookup
        self.announce   = announce
        self.allow_shrink = allow_shrink
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            import traceback
            self.detail.emit(traceback.format_exc())
            self.finished.emit(False, f'Import fehlgeschlagen: {e}. {KEEP_MSG}')

    def _run(self):
        if not self.field_map.get('strasse') or not self.field_map.get('hausnummer'):
            self.finished.emit(False, 'Straße und Hausnummer müssen '
                                       'zugeordnet werden.')
            return
        if not self.filepaths:
            self.finished.emit(False, no_addresses_msg())
            return

        sink = AddressSink(self.replace, self.rebuild_lookup, self.allow_shrink,
                           self.log.emit, self.detail.emit)
        counters = {'features': 0, 'skip': 0, 'total': 0}
        try:
            sink.start(self.bundesland)
            resolver = build_resolver_logged(log=self.log.emit, detail=self.detail.emit)
            multi = len(self.filepaths) > 1
            for path in self.filepaths:
                n_before = sink.n_ok
                try:
                    if not self._import_file(path, sink, resolver, counters):
                        sink.rollback()     # discard the deletion + inserts so far
                        self.finished.emit(False, CANCEL_MSG)
                        return
                except _ShpError as e:
                    sink.rollback()
                    self.finished.emit(False, f'{e} {KEEP_MSG}')
                    return
                if sink.n_ok == n_before:
                    sink.rollback()
                    self.finished.emit(False, no_addresses_msg(
                        os.path.basename(path) if multi else None))
                    return
            ok, msg = sink.finish(resolver)
            if not ok:
                self.finished.emit(False, msg)
                return
        except Exception:
            sink.rollback()     # discard DELETE + inserts so far completely
            raise               # run() reports the error; existing data is kept
        finally:
            sink.close()

        self.progress.emit(counters['features'], counters['total'], 'Fertig.')
        msg = f'Import abgeschlossen: {_n(sink.n_ok)} Adressen importiert'
        if counters['skip']:
            msg += f' ({_n(counters["skip"])} Objekte ohne verwertbare Adresse übersprungen)'
        self.finished.emit(True, msg + '.')

    def _import_file(self, path, sink, resolver, counters):
        """Imports one shapefile into the sink. Returns False on cancel;
        raises _ShpError for an unusable file."""
        (self.log if self.announce else self.detail).emit(f'Lese {os.path.basename(path)} …')
        self.detail.emit(f'Shapefile: {path}')

        layer = QgsVectorLayer(path, 'shp_import', 'ogr')
        if not layer.isValid():
            raise _ShpError(f'Das Shapefile {os.path.basename(path)} konnte nicht geöffnet werden.')

        total = layer.featureCount()
        counters['total'] += max(total, 0)
        self.log.emit(f'{_n(total)} Objekte im Shapefile.')

        f_strasse  = self.field_map.get('strasse')
        f_hausnr   = self.field_map.get('hausnummer')
        f_zusatz   = self.field_map.get('hausnr_zusatz')
        f_plz      = self.field_map.get('plz')
        f_gemeinde = self.field_map.get('gemeinde')
        names = set(layer.fields().names())
        missing = [f for f in (f_strasse, f_hausnr, f_zusatz, f_plz, f_gemeinde)
                   if f and f not in names]
        if missing:
            raise _ShpError(f'Im Shapefile {os.path.basename(path)} fehlen die Felder '
                            + ', '.join(missing) + '.')

        # ── Prepare the coordinate transformation ────────────────────────
        src_epsg = _layer_epsg(layer)
        if src_epsg is None:
            raise _ShpError('Das Koordinatensystem des Shapefiles ist nicht '
                            'bekannt (.prj-Datei fehlt?).')
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

        gem_cache = {}
        for feature in layer.getFeatures():
            if self._cancelled:
                return False

            counters['features'] += 1
            if counters['features'] % 5000 == 0:
                self.progress.emit(counters['features'], counters['total'],
                                   f'{_n(sink.n_ok)} Adressen eingelesen …')

            rp = _representative_point(feature.geometry())
            if rp is None:
                counters['skip'] += 1
                continue
            x, y = rp
            if transform is not None:
                try:
                    pt = transform.transform(QgsPointXY(x, y))
                    x, y = pt.x(), pt.y()
                except Exception:
                    counters['skip'] += 1
                    continue

            strasse = attr_text(feature[f_strasse])
            if not strasse:
                # building outline without an assigned address → skip
                counters['skip'] += 1
                continue

            zusatz = attr_text(feature[f_zusatz]) if f_zusatz else ''
            numbers = _hausnummer_rows(attr_text(feature[f_hausnr]), zusatz)
            if not numbers:
                counters['skip'] += 1     # implausible pseudo number / no number
                continue

            raw_gem = attr_text(feature[f_gemeinde]) if f_gemeinde else ''
            gemeinde_val = gem_cache.get(raw_gem)
            if gemeinde_val is None:
                gemeinde_val = gem_cache[raw_gem] = _clean_gemeinde(raw_gem) or self.bundesland

            plz_val = plz_text(feature[f_plz]) if f_plz else ''
            herkunft = None
            if not plz_val and resolver is not None:
                plz_val = resolver.lookup(x, y, target_epsg)
                herkunft = 'gebiet' if plz_val else None

            for hnr, adz in numbers:
                sink.add(self.bundesland, '', gemeinde_val, plz_val, strasse, hnr, adz,
                         target_epsg, x, y, herkunft)
        return True

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
        names = set(layer.fields().names())

        # Determine the geometry type for display
        gtype = QgsWkbTypes.geometryType(layer.wkbType())
        geom_label = {
            QgsWkbTypes.PointGeometry:   'Punkt (Adresspunkte)',
            QgsWkbTypes.PolygonGeometry: 'Polygon (Hausumringe — Flächenmittelpunkt wird verwendet)',
            QgsWkbTypes.LineGeometry:    'Linie',
        }.get(gtype, 'Unbekannt')

        previews = []
        for feature in layer.getFeatures():
            rp = _representative_point(feature.geometry())
            x, y = (rp if rp is not None else (None, None))

            def _attr(logical):
                fname = field_map.get(logical)
                if not fname or fname not in names:
                    return ''
                return attr_text(feature[fname])

            previews.append({
                'strasse':       _attr('strasse'),
                'hausnummer':    _attr('hausnummer'),
                'hausnr_zusatz': _attr('hausnr_zusatz'),
                'plz':           plz_text(_attr('plz')),
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
