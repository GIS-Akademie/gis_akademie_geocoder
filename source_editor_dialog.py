# -*- coding: utf-8 -*-
"""
Detail page of a source per Bundesland (tab „Datenbank & Quellen“, button
„Bearbeiten“ or double-click on the row).

This is where you define where the addresses of a Bundesland come from:
  * a file on this computer (HK-DE text, INSPIRE GML, shapefile or zip) —
    for states whose data is only available via a portal (shop, login);
  * an internet source (fixed file URL, directory index, link on a
    web page, feed, CKAN, WFS).
User-defined entries are stored in sources_user.json in the QGIS profile and
replace the Bundesland's default entry. From here the source can also be
updated individually or the file imported: the dialog then closes with
action = 'update', and the caller starts the download.
"""

import os
import zipfile

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal, Qt
from qgis.PyQt.QtWidgets import (QDialog, QFormLayout, QVBoxLayout, QHBoxLayout,
                                 QComboBox, QLineEdit, QLabel, QPushButton,
                                 QSpinBox, QMessageBox, QFileDialog, QCheckBox,
                                 QGroupBox, QWidget, QScrollArea, QFrame, QApplication)

from . import source_downloader as _sd


# Source types in the editor (file first — the most common case for portal states)
_EDIT_TYPES = list(_sd.EDITABLE_TYPES)

_TYPE_HELP = {
    'datei':      'Eine Adressdatei auf diesem Rechner oder einem Netzlaufwerk (HK-DE-Text, '
                  'INSPIRE-GML, Shapefile, OpenStreetMap-Extrakt oder ein Zip davon) — z. B. aus einem Landesportal mit '
                  'Anmeldung. „Alle eingetragenen Quellen aktualisieren“ importiert sie erneut, '
                  'sobald sie durch eine neuere Fassung ersetzt wurde.',
    'direct':     'Die URL zeigt direkt auf die Datei (.zip/.txt/.csv/.gml). '
                  'Ideal, wenn ein JavaScript-Portal eine stabile Download-URL hat '
                  '(in den Entwicklerwerkzeugen des Browsers unter „Netzwerk“ ablesbar).',
    'index_json': 'URL eines index.json im Format von opengeodata.nrw.de; das Muster '
                  'wählt die Datei aus der Liste.',
    'page_link':  'URL einer Webseite; das Muster (regulärer Ausdruck) findet den '
                  'Datei-Link im HTML. Geeignet, wenn sich der Link regelmäßig ändert.',
    'atom':       'URL eines Atom-Feeds; das Muster findet den Datei-Link im Feed.',
    'rss':        'URL eines RSS-Feeds; das Muster findet den Datei-Link im Feed.',
    'ckan':       'URL der CKAN-API (…/api/3/action/package_search); Abfrage und '
                  'Ressourcen-Muster wählen die jüngste passende Datei.',
    'wfs':        'URL eines WFS 2.0 mit INSPIRE-Adressen (ad:Address); die Seiten '
                  'werden einzeln geladen und als GML importiert.',
    'gaialight':  'Basisadresse eines GaiaLight-Downloaddienstes (…/_apps/dladownload) und das '
                  'Objekt (z.B. hk_alkis). Der neueste Bestand wird beim Landesamt als Auftrag '
                  'zusammengestellt und dann geladen.',
    'ogcapi':     'URL der Objektliste („…/collections/<Name>/items“) einer OGC-API-Features-'
                  'Schnittstelle; die Seiten werden nacheinander geladen. Die Feldzuordnung nennt '
                  'die Attribute für Straße, Hausnummer usw.',
    'index_geojson': 'URL eines GeoJSON mit einem Feature je Datei (z.B. je Kreis); das Feld '
                     'nennt die Datei-URL. Jede Datei wird geladen, umgewandelt und gleich wieder '
                     'gelöscht.',
}

_FORMAT_LABELS = {'hkde': 'HK-DE-Textformat (.txt/.csv)',
                  'gml':  'INSPIRE-GML',
                  'shp':  'Shapefile (Adresspunkte oder Hausumringe)',
                  'osm':  'OpenStreetMap-Extrakt (.osm.pbf)',
                  'alkis': 'ALKIS-GeoPackage (Gebäude und Flurstücke)',
                  'geojson': 'GeoJSON (OGC API Features)'}

_DEFAULT_ENTPACKEN = _sd._FILE_MUSTER      # same default patterns as for a file source

# Shapefile fields: key, label, required, name patterns for the preselection
_SHP_FIELDS = [
    ('strasse',       'Straße *',        True,  ['strassenna', 'straszenna', 'str_name', 'strname',
                                                 'strasse', 'straße', 'street', 'stn', 'str']),
    ('hausnummer',    'Hausnummer *',    True,  ['hausnummer', 'hnr', 'housenumbe', 'hsnr', 'nummer']),
    ('hausnr_zusatz', 'Hausnr.-Zusatz',  False, ['zusatz', 'adz', 'hnrzusatz', 'suffix']),
    ('plz',           'Postleitzahl',    False, ['postleitza', 'postcode', 'plz', 'zip']),
    ('gemeinde',      'Stadt / Ort',     False, ['gemeinde', 'ortsname', 'ort', 'stadt', 'city',
                                                 'gmd', 'place']),
]
_NONE = '— nicht zuordnen —'


def _fmt_int(n):
    try:
        return f'{int(n):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(n)


def _fmt_date(iso):
    if not iso:
        return ''
    try:
        y, m, d = iso[:10].split('-')
        return f'{d}.{m}.{y}'
    except Exception:
        return iso[:10]


def guess_format(path):
    """Data format from the file extension or zip contents ('hkde'|'gml'|'shp')."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pbf':
        return 'osm'
    if ext == '.shp':
        return 'shp'
    if ext in ('.gml', '.xml'):
        return 'gml'
    if ext == '.zip' and os.path.isfile(path):
        try:
            names = [n.lower() for n in zipfile.ZipFile(path).namelist()]
        except Exception:
            return 'hkde'
        if any(n.endswith('.shp') for n in names):
            return 'shp'
        if any(n.endswith(('.gml', '.xml')) for n in names):
            return 'gml'
    return 'hkde'


def _shp_uri(path):
    """Path/URI of the shapefile — for a zip, the first .shp inside it (/vsizip/)."""
    if path.lower().endswith('.zip'):
        try:
            member = next(n for n in zipfile.ZipFile(path).namelist() if n.lower().endswith('.shp'))
        except Exception:
            return None
        return f'/vsizip/{path}/{member}'
    return path


class _SourceTestWorker(QObject):
    done = pyqtSignal(bool, str)

    def __init__(self, entry, version):
        super().__init__()
        self.entry, self.version = entry, version

    def run(self):
        try:
            ok, text = _sd.test_source(self.entry, user_agent_version=self.version)
        except Exception as e:          # done must always arrive (the dialog waits for it)
            ok, text = False, f'{type(e).__name__}: {e}'
        self.done.emit(ok, text)


class SourceEditDialog(QDialog):
    """Detail page. After exec(): action ∈ {None, 'update', 'restored'} and
    force (for 'update': load even without new data)."""

    def __init__(self, parent, bundesland, entry=None, plugin_version='dev', standard=None,
                 meta=None, adressen=None, busy=False):
        super().__init__(parent)
        self.setWindowTitle(f'Quelle – {bundesland}')
        self.setMinimumWidth(680)
        self.bundesland = bundesland
        self.version    = plugin_version
        self.action     = None
        self.force      = False
        self._busy      = busy
        self._thread    = None
        self._worker    = None
        self._shp_fields_of = None          # path the field lists belong to
        entry = dict(entry or {})
        self._herkunft = entry.get('_herkunft', 'standard')
        self._standard = dict(standard or (entry if self._herkunft == 'standard' else {}))
        if not _sd.is_registered(entry):
            # No source registered: preset as file, take name/license/format
            # from the default entry
            entry = {'typ': 'datei', 'pfad': '',
                     'format': entry.get('format') if entry.get('format') in _FORMAT_LABELS else 'hkde',
                     'quelle_name': entry.get('quelle_name', ''), 'lizenz': entry.get('lizenz', '')}
        self._orig_entry = entry

        # Scrollable content, buttons fixed at the bottom — otherwise the dialog
        # gets squashed on small screens (e.g. the shapefile field mapping)
        dlg_layout = QVBoxLayout(self)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 4, 0)
        outer.setSpacing(8)
        self._scroll.setWidget(content)
        dlg_layout.addWidget(self._scroll, 1)
        self._content = content

        # ── Header: Bundesland and its status in the database ──────────────
        head = QLabel(f'<span style="font-size:15px;"><b>{bundesland}</b></span>')
        outer.addWidget(head)
        self.lbl_status = QLabel(self._status_text(meta, adressen))
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(self.lbl_status)

        # ── Default source ─────────────────────────────────────────────────
        grp_std = QGroupBox('Standardquelle')
        v_std = QVBoxLayout(grp_std)
        self.lbl_standard = QLabel(self._standard_text())
        self.lbl_standard.setWordWrap(True)
        self.lbl_standard.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_standard.setOpenExternalLinks(True)
        self.lbl_standard.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        v_std.addWidget(self.lbl_standard)
        if self._herkunft == 'eigene':
            row = QHBoxLayout()
            lbl = QLabel('<span style="color:#1565c0;">Für dieses Land ist eine eigene Quelle '
                         'eingetragen; sie ersetzt die Standardquelle.</span>')
            lbl.setWordWrap(True)
            row.addWidget(lbl, 1)
            self.btn_restore = QPushButton('Standardquelle wiederherstellen')
            self.btn_restore.clicked.connect(self._restore)
            row.addWidget(self.btn_restore)
            v_std.addLayout(row)
        outer.addWidget(grp_std)

        # ── Source ─────────────────────────────────────────────────────────
        grp = QGroupBox('Quelle für dieses Bundesland')
        form = QFormLayout(grp)
        self._form = form
        outer.addWidget(grp)

        self.cb_typ = QComboBox()
        for t in _EDIT_TYPES:
            self.cb_typ.addItem(_sd.TYPE_LABELS[t], t)
        form.addRow('Bezugsart', self.cb_typ)

        self.lbl_typ_help = QLabel()
        self.lbl_typ_help.setWordWrap(True)
        self.lbl_typ_help.setStyleSheet('color: gray; font-size: 10px;')
        form.addRow('', self.lbl_typ_help)

        # File
        self.lbl_pfad = QLabel('Datei *')
        self.w_pfad = QWidget()
        h = QHBoxLayout(self.w_pfad)
        h.setContentsMargins(0, 0, 0, 0)
        self.le_pfad = QLineEdit(entry.get('pfad', ''))
        self.le_pfad.setPlaceholderText('Adressdatei wählen (.txt, .csv, .gml, .shp, .osm.pbf oder .zip) …')
        self.btn_pfad = QPushButton('Durchsuchen …')
        h.addWidget(self.le_pfad, 1)
        h.addWidget(self.btn_pfad)
        form.addRow(self.lbl_pfad, self.w_pfad)

        # Internet
        self.lbl_url = QLabel('URL *')
        self.le_url = QLineEdit(entry.get('url', '') if _sd.is_automatic(entry) else '')
        self.le_url.setPlaceholderText('https://…')
        form.addRow(self.lbl_url, self.le_url)

        self.lbl_muster = QLabel('Muster')
        self.le_muster = QLineEdit()
        form.addRow(self.lbl_muster, self.le_muster)

        self.lbl_abfrage = QLabel('CKAN-Abfrage')
        self.le_abfrage = QLineEdit(entry.get('abfrage', ''))
        self.le_abfrage.setPlaceholderText('title:"ALKIS Adressen Hamburg"')
        form.addRow(self.lbl_abfrage, self.le_abfrage)

        self.lbl_typnamen = QLabel('WFS-Typnamen')
        self.le_typnamen = QLineEdit(', '.join(entry.get('typnamen', [])) or
                                     'ad:ThoroughfareName, ad:AdminUnitName, ad:Address')
        form.addRow(self.lbl_typnamen, self.le_typnamen)

        self.lbl_ebene = QLabel('Gemeinde-Ebene')
        self.cb_ebene = QComboBox()
        self.cb_ebene.addItem('automatisch (2. Verwaltungseinheit)', '')
        for lvl in ('2ndOrder', '3rdOrder', '4thOrder', '5thOrder', '6thOrder'):
            self.cb_ebene.addItem(f'INSPIRE {lvl}', lvl)
        form.addRow(self.lbl_ebene, self.cb_ebene)

        self.lbl_seite = QLabel('Seitengröße')
        self.sp_seite = QSpinBox()
        self.sp_seite.setRange(500, 100000)
        self.sp_seite.setSingleStep(1000)
        self.sp_seite.setValue(int(entry.get('seitengroesse', entry.get('max_objekte', 10000))))
        self.sp_seite.setToolTip('Seitengröße bzw. höchste Objektzahl je Anfrage. Viele Dienste '
                                 'liefern höchstens 10.000.')
        form.addRow(self.lbl_seite, self.sp_seite)

        self.lbl_abruf = QLabel('Abrufart')
        self.cb_abruf = QComboBox()
        self.cb_abruf.addItem('Seitenweise (count/startIndex)', 'seiten')
        self.cb_abruf.addItem('Räumliche Kacheln (BBOX) – bei instabilem Blättern', 'kacheln')
        self.cb_abruf.setToolTip(
            'Seitenweise genügt, wenn der Dienst stabil sortiert. Liefert er beim Blättern '
            'unvollständige Ergebnisse (der Abruf meldet dann „unvollständig“), Kacheln wählen.')
        form.addRow(self.lbl_abruf, self.cb_abruf)

        self.lbl_kachel_crs = QLabel('Kachel-Koordinatensystem')
        self.cb_kachel_crs = QComboBox()
        self.cb_kachel_crs.addItem('ETRS89 / UTM 32N (EPSG:25832)', 'urn:ogc:def:crs:EPSG::25832')
        self.cb_kachel_crs.addItem('ETRS89 / UTM 33N (EPSG:25833)', 'urn:ogc:def:crs:EPSG::25833')
        form.addRow(self.lbl_kachel_crs, self.cb_kachel_crs)

        # Common
        self.cb_format = QComboBox()
        form.addRow('Datenformat', self.cb_format)

        self.lbl_entpacken = QLabel('Auswahl im Zip')
        self.le_entpacken = QLineEdit(entry.get('entpacken', ''))
        self.le_entpacken.setPlaceholderText('leer = alle Dateien im Zip importieren')
        self.le_entpacken.setToolTip(
            'Nur für Downloads als Zip-Archiv: Muster (regulärer Ausdruck) auf die Dateinamen im '
            'Archiv, die importiert werden — z.B. ^gebref\\.txt$, damit Beschreibungen oder '
            'Lizenztexte im Zip ignoriert werden. Leer = alle Dateien.')
        form.addRow(self.lbl_entpacken, self.le_entpacken)

        # Shapefile field mapping
        self.grp_shp = QGroupBox('Shapefile-Feldzuordnung')
        f_shp = QFormLayout(self.grp_shp)
        self.shp_combos = {}
        for key, label, _req, _hints in _SHP_FIELDS:
            cb = QComboBox()
            cb.currentIndexChanged.connect(self._update_preview_shp)
            self.shp_combos[key] = cb
            f_shp.addRow(label, cb)
        n = QLabel('* Pflichtfelder — ohne Stadt/Ort wird das Bundesland als Ort verwendet; '
                   'fehlende PLZ werden automatisch ergänzt.')
        n.setWordWrap(True)
        n.setStyleSheet('color: gray; font-size: 10px;')
        f_shp.addRow(n)
        form.addRow(self.grp_shp)

        self.lbl_preview = QLabel('')
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_preview.setStyleSheet('color: gray; font-size: 11px;')
        form.addRow(self.lbl_preview)

        self.lbl_abstand = QLabel('Sperre nach Abruf')
        self.sp_abstand = QSpinBox()
        self.sp_abstand.setRange(0, 365)
        self.sp_abstand.setSuffix(' Tage')
        self.sp_abstand.setSpecialValueText('keine Sperre')
        self.sp_abstand.setValue(int(entry.get('min_abstand_tage', 0) or 0))
        self.sp_abstand.setToolTip(
            'Kein Zeitplan — das Plugin lädt nie von selbst. Beim Aktualisieren wird diese Quelle '
            'übersprungen, solange der letzte Abruf weniger Tage zurückliegt (sofort laden mit '
            '„Auch ohne neue Daten erneut laden“). Nur nötig, wenn der Server keinen Datenstand '
            'meldet (WFS) oder sich die Daten täglich ändern (OpenStreetMap) — sonst wird beim '
            'Server nachgefragt, ob es neue Daten gibt.')
        form.addRow(self.lbl_abstand, self.sp_abstand)

        self.le_name = QLineEdit(entry.get('quelle_name', ''))
        self.le_name.setPlaceholderText('z.B. HVBG – Hauskoordinaten Hessen')
        form.addRow('Bezeichnung', self.le_name)

        self.le_lizenz = QLineEdit(entry.get('lizenz', ''))
        self.le_lizenz.setPlaceholderText('z.B. dl-de/by-2-0')
        form.addRow('Lizenz', self.le_lizenz)

        self.le_hinweis = QLineEdit(entry.get('hinweis', ''))
        self.le_hinweis.setPlaceholderText('optional')
        form.addRow('Hinweis', self.le_hinweis)

        test_row = QHBoxLayout()
        self.btn_test = QPushButton('Quelle prüfen')
        self.btn_test.setToolTip('Prüft, ob die Datei bzw. der Dienst erreichbar ist — '
                                 'lädt nichts herunter und importiert nichts.')
        self.lbl_test = QLabel('')
        self.lbl_test.setWordWrap(True)
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self.lbl_test, 1)
        form.addRow(test_row)

        note = QLabel('Bitte nur Daten eintragen, die Sie nutzen dürfen, und bei Internetquellen '
                      'nur offen lizenzierte mit unmittelbarem Zugriff (keine Shops, Logins oder '
                      'Seiten mit Bot-Schutz). Eine eigene Quelle ersetzt die Standardquelle dieses '
                      'Bundeslands und übersteht Plugin-Updates.')
        note.setWordWrap(True)
        note.setStyleSheet('color: gray; font-size: 10px;')
        outer.addWidget(note)
        outer.addStretch(1)

        # ── Buttons ────────────────────────────────────────────────────────
        btns = QHBoxLayout()
        self.cb_force = QCheckBox('Auch ohne neue Daten erneut laden')
        self.cb_force.setToolTip('Normalerweise wird nur geladen, wenn sich der Stand der Quelle '
                                 'seit dem letzten Abruf geändert hat.')
        btns.addWidget(self.cb_force)
        btns.addStretch(1)
        self.btn_cancel = QPushButton('Abbrechen')
        self.btn_save = QPushButton('Speichern')
        self.btn_action = QPushButton('Jetzt aktualisieren')
        self.btn_action.setDefault(True)
        for b in (self.btn_cancel, self.btn_save, self.btn_action):
            btns.addWidget(b)
        dlg_layout.addLayout(btns)

        # ── Set values ─────────────────────────────────────────────────────
        i = self.cb_typ.findData(entry.get('typ', 'datei'))
        self.cb_typ.setCurrentIndex(max(i, 0))
        self._fill_formats(entry.get('format', 'hkde'))
        self.cb_ebene.setCurrentIndex(max(self.cb_ebene.findData(entry.get('gemeinde_ebene', '')), 0))
        self.cb_abruf.setCurrentIndex(max(self.cb_abruf.findData(entry.get('abruf', 'seiten')), 0))
        self.cb_kachel_crs.setCurrentIndex(
            max(self.cb_kachel_crs.findData(entry.get('kachel_crs', 'urn:ogc:def:crs:EPSG::25832')), 0))
        self._muster_values = {
            'index_json': entry.get('datei_muster', ''),
            'page_link':  entry.get('link_muster', ''),
            'atom':       entry.get('link_muster', ''),
            'rss':        entry.get('link_muster', ''),
            'ckan':       entry.get('ressource_muster', ''),
            'index_geojson': entry.get('url_feld', 'zip'),
            'gaialight':  entry.get('objekt', ''),
        }
        self._last_typ = None
        self._saved_felder = dict(entry.get('felder') or {})

        self.cb_typ.currentIndexChanged.connect(self._on_typ_changed)
        self.cb_abruf.currentIndexChanged.connect(self._on_typ_changed)
        self.cb_format.currentIndexChanged.connect(self._on_format_changed)
        self.le_pfad.editingFinished.connect(self._on_path_changed)
        self.btn_pfad.clicked.connect(self._browse)
        self.btn_test.clicked.connect(self._test)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_action.clicked.connect(self._on_action)
        self._on_typ_changed()
        if not self.le_entpacken.text():
            self._on_format_changed()
        if self.le_pfad.text():
            self._on_path_changed()
        # Initial state of the form: unchanged → save nothing (a default
        # source stays the default, even if it is only updated)
        self._initial = self.entry()
        self._sized = False

    def _show_row(self, field, visible):
        """Show/hide a form row — without a gap (Qt ≥ 6.4 / 5.x fallback)."""
        if hasattr(self._form, 'setRowVisible'):
            self._form.setRowVisible(field, visible)
        else:
            field.setVisible(visible)
            lbl = self._form.labelForField(field)
            if lbl is not None:
                lbl.setVisible(visible)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._sized:
            self._sized = True
            self._fit_size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_help()

    def _fit_help(self):
        """Make the wrapping help text exactly as tall as it needs at the current
        width — otherwise the form reserves space for a narrower width (gaps
        above and below the text)."""
        lbl = getattr(self, 'lbl_typ_help', None)
        if lbl is not None and lbl.width() > 50:
            lbl.setFixedHeight(lbl.heightForWidth(lbl.width()))

    def _fit_size(self):
        """As tall as the content, at most 90 % of the screen."""
        width = max(700, self.width())
        self.resize(width, self.height())
        self._fit_help()
        lay = self._content.layout()
        inner = width - 50
        h_content = (lay.totalHeightForWidth(inner) if lay.hasHeightForWidth()
                     else self._content.sizeHint().height())
        h = h_content + self.btn_action.sizeHint().height() + 40
        try:
            scr = (self.screen() if hasattr(self, 'screen') else None) or QApplication.primaryScreen()
            h = min(h, int(scr.availableGeometry().height() * 0.9))
        except Exception:
            pass
        self.resize(width, h)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _status_text(meta, adressen):
        if adressen:
            txt = f'In der Datenbank: <b>{_fmt_int(adressen)} Adressen</b>'
        else:
            txt = '<span style="color:#a15c00;">Noch keine Adressen in der Datenbank.</span>'
        if meta and meta.get('importiert_am'):
            txt += f' · zuletzt geladen am {_fmt_date(meta.get("importiert_am"))}'
            stand = _sd.format_last_modified(meta.get('last_modified'))
            if stand:
                txt += f' (Datenstand {stand})'
        return txt

    def _standard_text(self):
        e = self._standard
        if not e:
            return '<span style="color:gray;">Für dieses Bundesland gibt es keine Standardquelle.</span>'
        name = e.get('quelle_name', '')
        lizenz = e.get('lizenz', '')
        url = e.get('url', '')
        link = f'<a href="{url}">{url}</a>' if url else ''
        if _sd.is_automatic(e):
            lines = [f'<b>{name}</b> · Lizenz {lizenz} · wird direkt aus dem Internet geladen'
                     + (' (experimentell)' if e.get('status') == 'experimentell' else '')]
            if e.get('turnus'):
                lines.append(f'Aktualisierung bei der Quelle: {e["turnus"]}')
            if e.get('hinweis'):
                lines.append(f'<span style="color:gray;">{e["hinweis"]}</span>')
            if link:
                lines.append(link)
            return '<br>'.join(lines)
        lines = [f'<b>{name}</b> · Lizenz {lizenz}',
                 f'<span style="color:#a15c00;">Kein automatischer Download: {e.get("grund", "")}</span>']
        if link:
            lines.append(f'Daten herunterladen: {link}')
        lines.append('Die heruntergeladene Datei unten als <b>Datei auf diesem Rechner</b> eintragen '
                     'und importieren.')
        return '<br>'.join(lines)

    def _typ(self):
        return self.cb_typ.currentData()

    def _fill_formats(self, current=None):
        """Format list per source type (source_downloader.TYPE_FORMATS)."""
        current = current or self.cb_format.currentData()
        # Only the formats the import can take from this source type
        keys = [k for k in _sd.TYPE_FORMATS.get(self._typ(), ()) if k in _FORMAT_LABELS]
        self.cb_format.blockSignals(True)
        self.cb_format.clear()
        for k in keys:
            self.cb_format.addItem(_FORMAT_LABELS[k], k)
        self.cb_format.setCurrentIndex(max(self.cb_format.findData(current), 0))
        self.cb_format.blockSignals(False)

    def _on_typ_changed(self, *_a):
        if self._last_typ in self._muster_values:
            self._muster_values[self._last_typ] = self.le_muster.text()
        t = self._typ()
        typ_changed = t != self._last_typ
        self._last_typ = t
        is_file = t == 'datei'
        if typ_changed:
            self._fill_formats()
        self.lbl_typ_help.setText(_TYPE_HELP.get(t, ''))
        self._fit_help()
        self._show_row(self.w_pfad, is_file)
        for w in (self.le_url, self.sp_abstand):
            self._show_row(w, not is_file)
        muster_label = {'index_json': 'Dateiname-Muster *', 'page_link': 'Link-Muster *',
                        'atom': 'Link-Muster *', 'rss': 'Link-Muster *',
                        'ckan': 'Ressourcen-Muster *',
                        'index_geojson': 'Feld mit Datei-URL',
                        'gaialight': 'Objekt *'}.get(t)
        show_muster = muster_label is not None
        self._show_row(self.le_muster, show_muster)
        if show_muster:
            self.lbl_muster.setText(muster_label)
            self.le_muster.setText(self._muster_values.get(t, ''))
            self.le_muster.setPlaceholderText(
                r'z.B. https://[^"]+hk_[^"]+\.zip' if t != 'index_json' else r'z.B. ^hk_.*\.zip$')
        self._show_row(self.le_abfrage, t == 'ckan')
        for w in (self.le_typnamen, self.cb_ebene, self.cb_abruf):
            self._show_row(w, t == 'wfs')
        self._show_row(self.sp_seite, t in ('wfs', 'ogcapi'))
        self._show_row(self.cb_kachel_crs, t == 'wfs' and self.cb_abruf.currentData() == 'kacheln')
        if t == 'wfs':
            self.cb_format.setCurrentIndex(self.cb_format.findData('gml'))
            if not self.sp_abstand.value():
                self.sp_abstand.setValue(28)
        self.cb_format.setEnabled(t != 'wfs')
        self._show_row(self.lbl_preview, is_file)
        self.cb_force.setVisible(not is_file)
        self.btn_action.setText('Datei importieren' if is_file else 'Jetzt aktualisieren')
        self.btn_action.setToolTip(
            ('Speichert die Änderungen und importiert die Datei — der bisherige Bestand des '
             'Bundeslands wird ersetzt.') if is_file else
            ('Speichert die Änderungen und lädt die Quelle — nur wenn es neue Daten gibt (außer mit '
             '„Auch ohne neue Daten erneut laden“). Der bisherige Bestand wird ersetzt.'))
        if self._busy:
            self.btn_action.setEnabled(False)
            self.btn_action.setToolTip('Es läuft bereits ein Import. Bitte warten, bis er fertig ist.')
        self.lbl_test.setText('')
        self._on_format_changed()

    def _on_format_changed(self, *_a):
        cur = self.le_entpacken.text().strip()
        if not cur or cur in _DEFAULT_ENTPACKEN.values():
            self.le_entpacken.setText(_DEFAULT_ENTPACKEN.get(self.cb_format.currentData(), ''))
        is_shp = self.cb_format.currentData() == 'shp'
        is_ogc = self._typ() == 'ogcapi'
        # Field mapping: shapefiles, and the attributes of an OGC API Features source
        self._show_row(self.grp_shp, (is_shp and self._typ() != 'wfs') or is_ogc)
        self.grp_shp.setTitle('Feldzuordnung' if is_ogc else 'Shapefile-Feldzuordnung')
        if (is_shp and self._typ() not in ('datei', 'wfs')) or is_ogc:
            self._fill_shp_manual()
        # Selection within the zip only for internet downloads that can be a zip
        # (not for WFS, a local file or an OpenStreetMap extract)
        self._show_row(self.le_entpacken, self._typ() not in ('wfs', 'datei', 'ogcapi')
                       and self.cb_format.currentData() != 'osm')
        if self._typ() == 'datei':
            self._update_preview()

    # ── File ───────────────────────────────────────────────────────────────
    def _browse(self):
        start = os.path.dirname(self.le_pfad.text().strip()) if self.le_pfad.text().strip() else ''
        path, _ = QFileDialog.getOpenFileName(
            self, f'Adressdatei für {self.bundesland} wählen', start,
            'Adressdaten (*.txt *.csv *.gml *.xml *.shp *.zip *.pbf);;Alle Dateien (*)')
        if path:
            self.le_pfad.setText(path)
            self._on_path_changed()

    def _on_path_changed(self):
        path = self.le_pfad.text().strip()
        if path and os.path.isfile(path):
            fmt = guess_format(path)
            if self.cb_format.currentData() != fmt:
                self.cb_format.setCurrentIndex(max(self.cb_format.findData(fmt), 0))
            if not self.le_name.text().strip():
                self.le_name.setText(os.path.basename(path))
        self._update_preview()

    def _update_preview(self):
        if self._typ() != 'datei':
            return
        path = self.le_pfad.text().strip()
        if not path:
            self.lbl_preview.setText('Vorschau: bitte eine Datei wählen.')
            return
        if not os.path.isfile(path):
            self.lbl_preview.setText('<span style="color:#c62828;">Die Datei wurde nicht gefunden.</span>')
            return
        fmt = self.cb_format.currentData()
        try:
            if fmt == 'shp':
                self._load_shp_fields(path)
                self._update_preview_shp()
            elif fmt == 'osm':
                mb = os.path.getsize(path) / 1e6
                self.lbl_preview.setText(
                    f'OpenStreetMap-Extrakt ({mb:,.0f} MB) — die Adressen werden beim Import '
                    'umgewandelt; bei einem ganzen Bundesland dauert das einige Minuten.'
                    .replace(',', '.'))
            elif path.lower().endswith('.zip'):
                names = zipfile.ZipFile(path).namelist()
                self.lbl_preview.setText(f'Zip-Archiv mit {len(names)} Datei(en): '
                                         + ', '.join(os.path.basename(n) for n in names[:6])
                                         + (' …' if len(names) > 6 else '')
                                         + ' — die Adressdateien darin werden importiert.')
            elif fmt == 'gml':
                from .gml_import_worker import GmlImportWorker
                previews = GmlImportWorker.preview_file(path, n=2)
                if previews:
                    self.lbl_preview.setText('Vorschau: ' + ' · '.join(
                        f"Hausnummer {p['hausnummer']}, PLZ {p['plz'] or '–'}" for p in previews))
                else:
                    self.lbl_preview.setText('<span style="color:#c62828;">Keine Adressen im '
                                             'INSPIRE-GML-Format gefunden.</span>')
            else:
                from .db_import_worker import DbImportWorker
                previews = DbImportWorker.preview_file(path, n_lines=2)
                if not previews:
                    self.lbl_preview.setText('<span style="color:#c62828;">Keine lesbaren Adressen '
                                             'im HK-DE-Format gefunden.</span>')
                    return
                txt = 'Vorschau: ' + ' · '.join(
                    f"{p['strasse']} {p['hausnummer']}, {p['gemeinde']}" for p in previews)
                other = {p['bundesland'] for p in previews if p.get('bundesland')} - {self.bundesland}
                if other:
                    txt += (f'<br><span style="color:#a15c00;">Achtung: Die Datei enthält Adressen aus '
                            f'{", ".join(sorted(other))}. Sie werden als {self.bundesland} '
                            'importiert.</span>')
                self.lbl_preview.setText(txt)
        except Exception as e:
            self.lbl_preview.setText(f'<span style="color:#c62828;">Datei nicht lesbar: {e}</span>')

    def _load_shp_fields(self, path):
        if self._shp_fields_of == path:
            return
        for cb in self.shp_combos.values():
            cb.setEditable(False)
        uri = _shp_uri(path)
        fields, err = None, 'Kein Shapefile im Zip gefunden.'
        if uri:
            try:
                if uri == path:
                    from .shp_import_worker import get_layer_fields_with_error
                    fields, err = get_layer_fields_with_error(path)
                else:
                    from qgis.core import QgsVectorLayer
                    vl = QgsVectorLayer(uri, 'shp_fields', 'ogr')
                    fields = [f.name() for f in vl.fields()] if vl.isValid() else None
                    err = '' if fields else 'Das Shapefile im Zip ist nicht lesbar.'
            except Exception as e:
                fields, err = None, str(e)
        current = self._current_felder() or self._saved_felder
        for key, _label, required, hints in _SHP_FIELDS:
            cb = self.shp_combos[key]
            cb.blockSignals(True)
            cb.clear()
            if not required:
                cb.addItem(_NONE)
            cb.addItems(fields or [])
            want = current.get(key)
            idx = cb.findText(want) if want else -1
            if idx < 0 and fields:
                for cand in hints:
                    idx = next((i + (0 if required else 1) for i, f in enumerate(fields)
                                if cand in f.lower().replace('_', '')), -1)
                    if idx >= 0:
                        break
            if idx >= 0:
                cb.setCurrentIndex(idx)
            cb.blockSignals(False)
        self._shp_fields_of = path
        if fields is None:
            self.lbl_preview.setText(f'<span style="color:#c62828;">Shapefile nicht lesbar: {err}</span>')

    def _fill_shp_manual(self):
        """Internet source with a shapefile: the field names can only be read
        after the download — hence editable fields, prefilled with the stored
        mapping."""
        if self._shp_fields_of == '__manuell__':
            return
        current = self._current_felder() or self._saved_felder
        for key, _label, required, _hints in _SHP_FIELDS:
            cb = self.shp_combos[key]
            cb.blockSignals(True)
            cb.clear()
            cb.setEditable(True)
            if not required:
                cb.addItem(_NONE)
            if current.get(key):
                cb.addItem(current[key])
                cb.setCurrentIndex(cb.count() - 1)
            elif required:
                cb.setEditText('')
            cb.blockSignals(False)
        self._shp_fields_of = '__manuell__'

    def _current_felder(self):
        out = {}
        for key, _label, _req, _h in _SHP_FIELDS:
            t = self.shp_combos[key].currentText().strip()
            if t and t != _NONE:
                out[key] = t
        return out

    def _update_preview_shp(self, *_a):
        if self._typ() != 'datei' or self.cb_format.currentData() != 'shp':
            return
        path = self.le_pfad.text().strip()
        if self._shp_fields_of != path or not self.shp_combos['strasse'].count():
            return
        try:
            from .shp_import_worker import ShpImportWorker
            info = ShpImportWorker.preview_file(_shp_uri(path), field_map=self._current_felder(),
                                                n_features=2)
        except Exception:
            info = None
        if not info:
            self.lbl_preview.setText('Vorschau nicht möglich.')
            return
        rows = [f"{p['strasse']} {p['hausnummer']}{p['hausnr_zusatz'] or ''}, "
                + (f"{p['plz']} {p['gemeinde'] or self.bundesland}" if p['plz'] else
                   f"{p['gemeinde'] or self.bundesland} (PLZ wird ergänzt)")
                for p in info['previews']]
        self.lbl_preview.setText(f"Vorschau ({_fmt_int(info.get('feature_count', 0))} Objekte, "
                                 f"EPSG:{info.get('epsg')}): " + ' · '.join(rows))

    # ── Entry ──────────────────────────────────────────────────────────────
    # Catalog fields edited on this page; all others of the original entry
    # (e.g. ausweich, bbox, crs, dauer, vermerk) are kept unchanged.
    _FORM_KEYS = {'typ', 'format', 'quelle_name', 'lizenz', 'hinweis', 'felder', 'pfad', 'url',
                  'datei_muster', 'link_muster', 'ressource_muster', 'abfrage', 'url_feld',
                  'objekt', 'seitengroesse', 'typnamen', 'abruf', 'max_objekte', 'kachel_crs',
                  'kachel_bbox', 'gemeinde_ebene', 'entpacken', 'min_abstand_tage'}

    def entry(self):
        """Catalog entry from the form fields — plus the fields of the original
        entry that this page does not show (if the source type is unchanged)."""
        t = self._typ()
        orig = self._orig_entry or {}
        same_typ = orig.get('typ') == t
        e = {k: v for k, v in orig.items()
             if same_typ and k not in self._FORM_KEYS and not str(k).startswith('_')}
        e.update({'typ': t, 'format': self.cb_format.currentData(),
                  'quelle_name': self.le_name.text().strip() or f'Eigene Quelle {self.bundesland}',
                  'lizenz': self.le_lizenz.text().strip(),
                  'hinweis': self.le_hinweis.text().strip()})
        if e['format'] == 'shp' or t == 'ogcapi':
            e['felder'] = self._current_felder() or dict(self._saved_felder)
        if t == 'datei':
            e['pfad'] = self.le_pfad.text().strip()
            return {k: v for k, v in e.items() if v not in ('', None, [], {})}
        e['url'] = self.le_url.text().strip()
        muster = self.le_muster.text().strip()
        if t == 'index_json':
            e['datei_muster'] = muster
        elif t in ('page_link', 'atom', 'rss'):
            e['link_muster'] = muster
        elif t == 'ckan':
            e['ressource_muster'] = muster
            e['abfrage'] = self.le_abfrage.text().strip()
        elif t == 'index_geojson':
            e['url_feld'] = muster or 'zip'
        elif t == 'gaialight':
            e['objekt'] = muster
        elif t == 'ogcapi':
            e['seitengroesse'] = self.sp_seite.value()
        if t == 'wfs':
            e['typnamen'] = [x.strip() for x in self.le_typnamen.text().split(',') if x.strip()]
            if self.cb_abruf.currentData() == 'kacheln':
                e['abruf'] = 'kacheln'
                e['max_objekte'] = self.sp_seite.value()
                e['kachel_crs'] = self.cb_kachel_crs.currentData()
                # The tile frame of the original entry stays (it belongs to its
                # coordinate system); otherwise a generous frame covering Germany
                # in UTM 32 and 33 — empty tiles only cost a hit-count query.
                e['kachel_bbox'] = (orig.get('kachel_bbox')
                                    if same_typ and orig.get('kachel_crs') == e['kachel_crs']
                                    and orig.get('kachel_bbox') else [-500000, 5000000, 1500000, 6500000])
            else:
                e['seitengroesse'] = self.sp_seite.value()
            if self.cb_ebene.currentData():
                e['gemeinde_ebene'] = self.cb_ebene.currentData()
        elif t != 'ogcapi' and self.le_entpacken.text().strip():
            e['entpacken'] = self.le_entpacken.text().strip()
        if self.sp_abstand.value():
            e['min_abstand_tage'] = self.sp_abstand.value()
        return {k: v for k, v in e.items() if v not in ('', None, [], {})}

    def changed(self):
        return self.entry() != self._initial

    def _persist(self):
        """Save changes. Unchanged → do nothing (the default source stays
        the default). Returns False if the entry is invalid."""
        if not self.changed():
            return True
        try:
            _sd.save_user_source(self.bundesland, self.entry())
        except Exception as e:
            QMessageBox.warning(self, 'Quelle unvollständig', str(e))
            return False
        return True

    def _on_save(self):
        if self._persist():
            self.accept()

    def _on_action(self):
        effective = self.entry() if self.changed() else self._orig_entry
        if not _sd.is_registered(effective) or (self._typ() == 'datei' and not effective.get('pfad')):
            QMessageBox.information(self, 'Keine Quelle eingetragen',
                                    'Bitte zuerst eine Datei wählen oder eine Internetquelle eintragen.')
            return
        if self._typ() == 'datei' and not os.path.isfile(self.le_pfad.text().strip()):
            QMessageBox.warning(self, 'Datei nicht gefunden', 'Die gewählte Datei wurde nicht gefunden.')
            return
        if not self._persist():
            return
        self.action = 'update'
        # A file is always imported when explicitly requested
        self.force = self._typ() == 'datei' or self.cb_force.isChecked()
        self.accept()

    def _restore(self):
        if QMessageBox.question(
                self, 'Standardquelle wiederherstellen',
                f'Eigene Quelle für {self.bundesland} entfernen? Danach gilt wieder die '
                'Standardquelle. Die Adressen in der Datenbank bleiben erhalten.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            _sd.remove_user_source(self.bundesland)
        except Exception as e:
            QMessageBox.warning(self, 'Fehler', str(e))
            return
        self.action = 'restored'
        self.accept()

    # ── Test ───────────────────────────────────────────────────────────────
    def _test(self):
        if self._thread is not None and self._thread.isRunning():
            return
        self.btn_test.setEnabled(False)
        self.lbl_test.setStyleSheet('')
        self.lbl_test.setText('Prüfe …')
        self._thread = QThread(self)
        self._worker = _SourceTestWorker(self.entry(), self.version)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_test_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _on_test_done(self, ok, text):
        self.btn_test.setEnabled(True)
        self.lbl_test.setStyleSheet('color: #2e7d32;' if ok else 'color: #c62828;')
        self.lbl_test.setText(('✓ ' if ok else '✗ ') + text)

    def done(self, r):
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(35000)
        super().done(r)
