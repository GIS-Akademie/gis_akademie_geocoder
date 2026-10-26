# -*- coding: utf-8 -*-

import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsSettings, QgsApplication
from .gis_akademie_geocoder_dialog import (GISAkademieGeocoderDialog, SETTINGS_KEY,
                                            PLZ_SETTINGS_KEY)
from .local_geocoder import set_db_path
from .source_downloader import set_user_catalog_path, set_online_catalog_path
from . import plz_resolver


def _profile_file(name):
    """<QGIS profile>/gis_akademie_geocoder/<name> — the profile survives plugin
    updates (QGIS deletes the plugin folder when updating)."""
    try:
        base = QgsApplication.qgisSettingsDirPath()
    except Exception:
        base = ''
    base = base or os.path.expanduser('~')
    # normpath: QGIS returns the profile with '/', os.path.join appends '\'
    return os.path.normpath(os.path.join(base, 'gis_akademie_geocoder', name))


def _default_db_path():
    return _profile_file('geocoder.db')


def resolve_db_path(settings):
    """Active DB path: the path stored in QgsSettings (chosen by the user),
    otherwise the default in the QGIS profile. A stored path inside the
    plugin folder is not used (QGIS deletes that folder on every update)."""
    stored = str(settings.value(SETTINGS_KEY, '') or '')
    plugin_dir = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
    if stored and os.path.normcase(os.path.abspath(stored)).startswith(plugin_dir + os.sep):
        settings.remove(SETTINGS_KEY)
        stored = ''
    return stored or _default_db_path()


class GISAkademieGeocoderPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self._db_warning = None

        # DB path (setting → default in the QGIS profile). If the stored folder
        # cannot be reached (e.g. a network drive that is not connected yet),
        # the default is used for this session — the plugin must still load
        # so that the path can be changed.
        path = resolve_db_path(QgsSettings())
        try:
            set_db_path(path)
        except OSError as e:
            set_db_path(_default_db_path())
            self._db_warning = (f'Der gespeicherte Datenbankpfad {path} ist nicht erreichbar ({e}). '
                                'Für diese Sitzung wird die Datenbank im QGIS-Profil verwendet.')
        # Store user-defined sources (tab „Datenbank & Quellen“) in the QGIS
        # profile — there they survive plugin updates.
        set_user_catalog_path(_profile_file('sources_user.json'))
        # Source list delivered online later (next to sources_user.json)
        set_online_catalog_path(_profile_file('sources_online.json'))
        # User-defined postcode (PLZ) areas (tab „Datenbank & Quellen“),
        # otherwise the bundled ones
        try:
            import json
            cfg = QgsSettings().value(PLZ_SETTINGS_KEY, '')
            plz_resolver.set_override(json.loads(cfg) if cfg else None)
        except Exception:
            plz_resolver.set_override(None)

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icons', 'icon.png')
        self.action = QAction(
            QIcon(icon_path),
            'GIS-Akademie Geocoder',
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu('&GIS-Akademie Geocoder', self.action)

    def unload(self):
        # Stop running worker threads before the dialog is destroyed —
        # otherwise 'QThread: Destroyed while thread is still running' and a
        # crash when reloading/uninstalling the plugin during an import.
        if self.dialog is not None:
            try:
                self.dialog.shutdown()
                self.dialog.close()
                self.dialog.deleteLater()
            except Exception:
                pass
            self.dialog = None
        self.iface.removePluginVectorMenu('&GIS-Akademie Geocoder', self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        if self.dialog is None:
            self.dialog = GISAkademieGeocoderDialog(self.iface)
        if self._db_warning:
            try:
                self.iface.messageBar().pushWarning('GIS-Akademie Geocoder', self._db_warning)
            except Exception:
                pass
            self._db_warning = None
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
