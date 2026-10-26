# -*- coding: utf-8 -*-

import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsSettings, QgsApplication
from .gis_akademie_geocoder_dialog import GISAkademieGeocoderDialog
from .local_geocoder import set_db_path
from .source_downloader import set_user_catalog_path, set_online_catalog_path
from . import plz_resolver

SETTINGS_KEY = 'gis_akademie_geocoder/db_path'


def _legacy_db_path():
    """Former default path: <plugin directory>/geocoder.db. The database is
    lost there on every plugin update, because QGIS deletes and recreates the
    plugin folder when updating."""
    return os.path.join(os.path.dirname(__file__), 'geocoder.db')


def _default_db_path():
    """Default path for the geocoding database: in the QGIS profile
    (<profile>/gis_akademie_geocoder/geocoder.db). The profile survives plugin
    updates. Falls back to the plugin folder if QGIS provides no profile
    directory (e.g. outside a running QGIS instance)."""
    try:
        profile_dir = QgsApplication.qgisSettingsDirPath()
    except Exception:
        profile_dir = ''
    if profile_dir:
        # normpath: QGIS returns the profile with '/', os.path.join appends '\'
        return os.path.normpath(os.path.join(profile_dir, 'gis_akademie_geocoder', 'geocoder.db'))
    return _legacy_db_path()


def _user_catalog_path():
    """<QGIS profile>/gis_akademie_geocoder/sources_user.json"""
    try:
        profile_dir = QgsApplication.qgisSettingsDirPath()
    except Exception:
        profile_dir = ''
    base = profile_dir or os.path.dirname(__file__)
    return os.path.normpath(os.path.join(base, 'gis_akademie_geocoder', 'sources_user.json'))


def resolve_db_path(settings):
    """Determine the active DB path:
      1. the path stored in QgsSettings (chosen by the user),
      2. otherwise an existing database at the old location in the plugin
         folder — it keeps being used and the path is stored as an explicit
         setting, so that it is visible in the admin tab and a later update
         does not silently forget it,
      3. otherwise the new default in the QGIS profile."""
    stored = settings.value(SETTINGS_KEY, '')
    if stored:
        return str(stored)
    legacy = _legacy_db_path()
    if os.path.exists(legacy):
        settings.setValue(SETTINGS_KEY, legacy)
        return legacy
    return _default_db_path()


class GISAkademieGeocoderPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

        # Determine the DB path (setting → old plugin folder → QGIS profile)
        set_db_path(resolve_db_path(QgsSettings()))
        # Store user-defined sources (tab „Datenbank & Quellen“) in the QGIS
        # profile — there they survive plugin updates.
        set_user_catalog_path(_user_catalog_path())
        # Source list delivered online later (next to sources_user.json)
        set_online_catalog_path(os.path.join(os.path.dirname(_user_catalog_path()),
                                             'sources_online.json'))
        # User-defined postcode (PLZ) areas (tab „Datenbank & Quellen“),
        # otherwise the bundled ones
        try:
            import json
            cfg = QgsSettings().value('gis_akademie_geocoder/plz_quelle', '')
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
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
