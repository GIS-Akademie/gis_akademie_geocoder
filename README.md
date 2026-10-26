# GIS-Akademie Geocoder (QGIS-Plugin)

*English:* QGIS plugin that geocodes German address tables (CSV, Excel, OpenDocument, tab-separated) against a local
SQLite database of official house coordinates for all 16 German states, with Nominatim as an optional
fallback. It can work fully offline (GDPR mode). The plugin downloads and updates the states' open data
itself, converts an OpenStreetMap extract for Bavaria and adds missing postcodes from bundled postcode
areas derived from OpenStreetMap. User interface and documentation are in German. Install it from the
QGIS plugin manager; report problems in the [issue tracker](https://github.com/GIS-Akademie/gis_akademie_geocoder/issues).

Geocodiert Adressen aus CSV-, Excel-, OpenDocument- oder Tab-Dateien gegen eine lokale SQLite-Datenbank
amtlicher Hauskoordinaten (alle 16 Bundesländer) mit Nominatim als optionalem Fallback.
Ergebnis ist ein GeoPackage-Layer in QGIS. Details, Datenquellen und Lizenzen: `help.html`
(im Plugin über „Hilfe“).

## Aufbau

| Datei | Zweck |
|---|---|
| `local_geocoder.py` | Lokale Suche (SQLite, Levenshtein), Normalisierung, Bundesland-Erkennung, Gemeinde-/Ortsteil-Verzeichnis, Quellen-Metadaten. **QGIS-frei**, daher direkt testbar. |
| `geocoder_worker.py` | Hintergrund-Worker: lokale Suche → Nominatim-Fallback, Genauigkeitsstufen. |
| `gis_akademie_geocoder_dialog.py` / `.ui` | Dialog: Datei laden, Spaltenzuordnung, GeoPackage-Ausgabe, Admin-/Import-Tab. |
| `db_import_worker.py`, `gml_import_worker.py`, `shp_import_worker.py` | Import von HK-DE-Text, INSPIRE-GML (Datei oder WFS-Seiten) und Shapefiles in die Datenbank (je eine Transaktion). |
| `sources.json`, `source_downloader.py`, `source_update_worker.py`, `source_editor_dialog.py` | Quellenkatalog, Abruf (einzeln oder alle, nur bei neuen Daten), Import, Stand-Verwaltung (`source_meta`) und Detailseite je Bundesland für eigene Quellen (`sources_user.json` im QGIS-Profil). |
| `osm_convert.py` | OpenStreetMap-Extrakt (.osm.pbf) per GDAL-OSM-Treiber in eine HK-BY-Datei umwandeln (Bayern; Format `osm` im Katalog). |
| `plz_resolver.py`, `data/plz_gebiete.*` | Mitgelieferte PLZ-Gebiete (direkt aus OpenStreetMap, ODbL) und automatische PLZ-Ergänzung per Punkt-in-Fläche: beim Import und im Nachlauf `ergaenze_plz()`; mit QGIS oder in reinem Python. |
| `tools/build_db.py` | Baut die Datenbank ohne QGIS aus allen automatisch abrufbaren Quellen. |
| `tools/build_plz_gebiete.py` | Erzeugt `data/plz_gebiete.geojson` neu aus den OSM-Relationen `boundary=postal_code` des Geofabrik-Deutschland-Extrakts (mit der Python-Umgebung von QGIS starten, braucht GDAL). |
| `tools/` (weitere) | Diagnose- und Migrationsskripte für bestehende Datenbanken, Qt-Stub. |
| `tests/` | pytest-Suite (läuft ohne QGIS). |

Die Geocodierungsdatenbank liegt **nicht** im Repository (siehe `.gitignore`). Standardpfad ist
`<QGIS-Profil>/gis_akademie_geocoder/geocoder.db`; ein anderer Pfad wird im Reiter „Datenbank & Quellen“ gesetzt.
Automatisch geladene Quelldateien landen im Ordner `quellen/` neben der Datenbank.

## Automatischer Quellen-Download

`sources.json` beschreibt je Bundesland, wie die Hauskoordinaten bezogen werden:

| Typ | Bedeutung | Beispiel |
|---|---|---|
| `direct` | feste Datei-URL, optional mit Platzhaltern `{datum}` (JJJJMMTT), `{jahr}`, `{nr}` (Datenfassung), die beim Abruf aufgelöst werden | Hessen, Bremen, Bayern |
| `index_json` | Verzeichnisindex von opengeodata.nrw.de | Nordrhein-Westfalen |
| `atom` / `rss` / `page_link` | Datei-Link per Regex aus Feed oder HTML-Seite | Berlin, Sachsen |
| `ckan` | CKAN-API `package_search`, jüngste passende Ressource | – (für eigene Quellen) |
| `wfs` | WFS 2.0 GetFeature mit Paging, Import der GML-Seiten | Mecklenburg-Vorpommern, Hamburg |
| `datei` | eingetragene Datei auf diesem Rechner (`pfad`; HK-DE, GML, Shapefile mit `felder` oder Zip); erneuter Import, wenn Größe/Änderungszeit sich ändern | nur eigene Quellen |
| `index_geojson` | GeoJSON-Index mit einer Datei je Feature (Feld `url_feld`); jede Datei wird geladen, umgewandelt und gelöscht | Niedersachsen (Format `alkis`) |
| `gaialight` | GaiaLight-Downloaddienst: neuesten Bestand ermitteln, Auftrag starten, warten, laden (`objekt`) | Schleswig-Holstein |
| `ogcapi` | OGC API Features, GeoJSON-Seiten mit Feldzuordnung `felder` | Saarland |
| `manuell` | kein automatischer Abruf (Shop, Registrierung, Bot-Schutz, Aufbereitung) – mit Begründung | Hessen, RLP, Thüringen … |

Meldungen: Die Worker haben zwei Kanäle – `log` für das Protokollfenster (Texte für Endkunden)
und `detail` für technische Angaben, die der Dialog ins QGIS-Meldungsprotokoll (Reiter
„GIS-Akademie Geocoder“) schreibt. `SourceError` trägt eine kundentaugliche Meldung und
optional `detail`. Im Katalog sind `hinweis`/`grund` für Endkunden, `technik` wird nie angezeigt.
Der Test `test_user_log_has_no_technical_terms` sichert die Trennung ab.

Es werden nur offen lizenzierte Quellen mit unmittelbarem Zugriff abgerufen (sprechender
User-Agent mit Kontaktadresse). Der letzte Stand (ETag/Last-Modified, Prüfsumme, Datum,
Adresszahl) steht in der Tabelle `source_meta`; ein erneuter Abruf fragt bedingt an.

Eine neue Quelle braucht nur einen Katalogeintrag (Typ, URL, Regex, Format). Optional: `ausweich` (Ausweichquelle,
wenn die Hauptquelle scheitert; mit `"nur_ohne_bestand": true` nur, solange das Land noch keine
Adressen hat) und `vermerk` (Quellenvermerk). Nach jeder Änderung an `sources.json`
`python tools/build_help_tables.py` ausführen — es erzeugt die Quellentabellen der Hilfe; `build_zip.py`
prüft, dass sie aktuell sind. Für INSPIRE-GML
aus einem WFS gibt `gemeinde_ebene` an, welche INSPIRE-Verwaltungsebene als Gemeinde gilt;
`min_abstand_tage` ist eine Sperre nach dem Abruf (kein Zeitplan): beim Aktualisieren wird die
Quelle übersprungen (Status `skipped`), solange der letzte Abruf jünger ist — für Quellen ohne ETag
(WFS) oder mit täglich neuem Stand (OSM); `liefert_plz` steuert den PLZ-Hinweis.

**Quellenliste online pflegen:** Das Plugin lädt `sources.json` von `katalog_url` (Standard:
`main`-Zweig dieses Repositorys über raw.githubusercontent.com — das Repository muss dafür öffentlich
sein) und verwendet sie, wenn ihr `stand` neuer ist als der mitgelieferte. Für eine Link-Korrektur
also nur `sources.json` ändern, **`stand` auf das heutige Datum setzen** und auf `main` pushen — keine
neue Plugin-Version nötig. Neue Abruf-Typen oder Formate brauchen dagegen eine neue Plugin-Version;
ältere Plugins übergehen solche Einträge. `version` (Format der Liste) nur erhöhen, wenn alte Plugins
die Liste gar nicht mehr lesen sollen.

Nutzer können im Reiter „Datenbank & Quellen“ über die Detailseite eines Bundeslands eigene
Quellen eintragen – eine Internetquelle oder eine Datei (`typ: datei`). Sie werden in
`<QGIS-Profil>/gis_akademie_geocoder/sources_user.json` gespeichert und ersetzen den
Standardeintrag des Bundeslands (`source_downloader.load_catalog` führt beide zusammen).

## Datenbank ohne QGIS bauen

```bash
python tools/build_db.py --list
python tools/build_db.py C:/Daten/geocoder.db
python tools/build_db.py C:/Daten/geocoder.db --bundesland Berlin Sachsen --force
```

Fehlende PLZ werden auch dort aus den mitgelieferten PLZ-Gebieten ergänzt (reiner Python-Rechenweg,
rund 30.000 Adressen je Sekunde).

## Tests

Die Suite braucht nur Python 3 und pytest, kein QGIS (`qgis.PyQt.QtCore` wird durch einen Stub
aus `tools/_qt_stub.py` ersetzt). Sie baut eine Mini-Datenbank mit gleichnamigen Orten,
Ortsteilen und Hausnummern-Zusätzen auf und prüft Normalisierung, Bundesland-Erkennung,
Suche, Straßen-Repräsentant, Diagnose, Worker, Quellenkatalog, Download (mit Fake-HTTP),
WFS-Paging und GML-Import.

```bash
python -m pytest tests -q
```

## Deploy

Vor jeder Auslieferung die Version in `metadata.txt` erhöhen (QGIS ersetzt ein Plugin gleicher
Version nicht zuverlässig) und den Changelog ergänzen. Zum Verpacken den Plugin-Ordner **ohne**
`material/`, `tests/`, `tools/`, `__pycache__/` zippen (`sources.json` muss enthalten sein).

## Lizenz

- **Code:** GNU General Public License, Version 2 oder später (GPL-2.0-or-later), siehe `LICENSE`.
- **Mitgelieferte PLZ-Gebiete** (`data/plz_gebiete.geojson`): © OpenStreetMap-Mitwirkende,
  Open Database License (ODbL) 1.0.
- **Geladene Adressdaten:** Lizenz der jeweiligen Quelle (meist dl-de/by-2-0, Berlin dl-de/zero-2-0,
  Bremen CC BY 4.0, Bayern/OpenStreetMap ODbL) — Namensnennung siehe Hilfe (`help.html`).

## Veröffentlichung im QGIS-Plugin-Verzeichnis

```
python tools/build_zip.py          # Paket bauen und nach den Regeln von plugins.qgis.org prüfen
python tools/build_zip.py --urls   # zusätzlich repository/tracker/homepage abrufen
```

Voraussetzungen für den Upload: das Repository ist **öffentlich** (das Verzeichnis ruft repository,
tracker und homepage beim Hochladen ab), Issues sind aktiviert, die Version in `metadata.txt` ist
erhöht. Die Sicherheitsprüfung des Verzeichnisses (Bandit, detect-secrets) ergab für 1.0 keine
kritischen Befunde.

