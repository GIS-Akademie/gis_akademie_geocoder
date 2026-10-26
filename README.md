# GIS-Akademie Geocoder (QGIS-Plugin)

*English:* QGIS plugin that geocodes German address tables (CSV, Excel, OpenDocument, tab-separated) against a local
database of official house coordinates for all 16 German states, with Nominatim as an optional
fallback. It can work fully offline (GDPR mode). The plugin downloads and updates the states' open data
itself, converts an OpenStreetMap extract for Bavaria and adds missing postcodes from bundled postcode
areas derived from OpenStreetMap. User interface and documentation are in German. Install it from the
QGIS plugin manager; report problems in the [issue tracker](https://github.com/GIS-Akademie/gis_akademie_geocoder/issues).

Das Plugin ordnet Adressen aus Tabellendateien Koordinaten zu und legt das Ergebnis als Punkt-Layer in
einem GeoPackage ab. Grundlage ist eine lokale Datenbank amtlicher Hauskoordinaten aller
16 Bundesländer, die das Plugin selbst aus den offenen Daten der Länder aufbaut.

## Funktionen

- **Eingabe:** CSV, Excel (.xlsx/.xls), OpenDocument-Tabellen (.ods) und Tab-getrennte Textdateien.
  Die Spalten (Straße, Hausnummer, PLZ, Ort …) werden zugeordnet; auch eine einzelne Spalte mit der
  ganzen Adresse ist möglich.
- **Lokale Suche:** fehlertolerant bei Tippfehlern, Abkürzungen („Str.“) und ß/ss; Ortsteile und
  Bezirke werden erkannt.
- **Nominatim (OpenStreetMap)** optional als Ausweichsuche für Adressen ohne lokalen Treffer.
- **DSGVO-Modus:** nur die lokale Datenbank, keine Adresse verlässt den Rechner.
- **Ergebnis:** GeoPackage-Layer mit Genauigkeitsstufe und Übereinstimmungswert je Adresse; nicht
  gefundene Adressen wahlweise als eigene CSV-Datei.
- **Datenbank im Plugin:** Ein Klick lädt die offenen Daten aller Länder und aktualisiert später nur,
  was neu ist. Ändert ein Land seinen Download, kommt die neue Adresse über eine online gepflegte
  Quellenliste – ohne neue Plugin-Version.
- **Postleitzahlen:** Fehlende PLZ werden aus mitgelieferten PLZ-Gebieten (OpenStreetMap) ergänzt.

## Installation

In QGIS über *Erweiterungen → Erweiterungen verwalten und installieren* nach „GIS-Akademie Geocoder“
suchen und installieren. Weitere Pakete sind nicht nötig; nur für das alte Excel-Format .xls wird das
Python-Paket `xlrd` gebraucht.

## Erste Schritte

1. **Datenbank einrichten (einmalig):** Reiter „Datenbank & Quellen“ öffnen und *Alle eingetragenen
   Quellen aktualisieren* klicken. Für alle 16 Länder dauert das beim ersten Mal rund 2–2,5 Stunden
   (rund 11 GB Download, fertige Datenbank 5–6 GB). Wer nur Adressen aus wenigen Ländern hat, lädt
   nur diese.
2. **Geocodieren:** Im Reiter „Geocodierung“ die Datei wählen, die Spaltenzuordnung prüfen, Ziel und
   Layer-Name angeben und *Geocodierung starten* klicken.
3. **Ergebnis prüfen:** Die Felder `accuracy_level` (Genauigkeit) und `local_score` (Übereinstimmung)
   zeigen, wie gut jeder Treffer ist.

Die ausführliche Anleitung mit Aufwand je Bundesland, Quellen und Fehlerbehebung öffnet sich im Plugin
über *Hilfe*. Eine fertige, monatlich aktualisierte Datenbank bietet die GIS-Akademie als
[Geodatenabo Plus](https://www.gis-akademie.de/gis/geodatenabo.html) an; das Plugin funktioniert
auch ohne Abo vollständig.

## Änderungsprotokoll

<!-- AENDERUNGEN -->
- **1.0** – Erste Veröffentlichung im QGIS-Plugin-Verzeichnis. Geocodiert Adresstabellen (CSV, Excel, OpenDocument, Tab-getrennt) gegen eine lokale Datenbank amtlicher Hauskoordinaten aller 16 Bundesländer, optional mit Nominatim als Ausweichsuche oder vollständig offline (DSGVO-Modus). Die Datenbank wird im Plugin eingerichtet und aktualisiert: Ein Klick lädt die offenen Daten der Länder und importiert nur, was sich geändert hat; die Quellenliste lässt sich online aktualisieren. Fehlende Postleitzahlen werden aus den mitgelieferten PLZ-Gebieten (OpenStreetMap) ergänzt.
<!-- /AENDERUNGEN -->

## Lizenzen

- **Plugin:** GNU General Public License, Version 2 oder später (GPL-2.0-or-later), siehe `LICENSE`.
- **Mitgelieferte PLZ-Gebiete:** © OpenStreetMap-Mitwirkende, Open Database License (ODbL) 1.0.
- **Geladene Adressdaten:** Lizenz der jeweiligen Quelle (meist dl-de/by-2-0, Berlin dl-de/zero-2-0,
  Bremen und weitere CC BY 4.0, Bayern/OpenStreetMap ODbL). Die Quellenvermerke je Bundesland stehen
  in der Hilfe und sind bei Weitergabe von Ergebnissen anzugeben.

## Fehler melden

Fehler und Wünsche bitte im [Issue-Tracker](https://github.com/GIS-Akademie/gis_akademie_geocoder/issues)
melden. Technische Angaben zu einem Fehler stehen in QGIS unter *Ansicht → Bedienfelder →
Protokollmeldungen*, Reiter „GIS-Akademie Geocoder“.
