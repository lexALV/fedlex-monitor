# Fedlex-Monitor für in Kraft tretende SR-Änderungen

Der Monitor fragt die Fedlex-Datenplattform zweimal per HTTP POST ab, verknüpft
die konsolidierten Fassungen mit den ändernden Erlassen der Amtlichen Sammlung
und erzeugt statische Dateien auf Deutsch, Französisch und Italienisch für
WebSite-Watcher (WSW). Beide Abfragen liefern alle drei Sprachen gemeinsam; die
Zahl der Anfragen erhöht sich durch die Mehrsprachigkeit deshalb nicht.

- `site/index.html` und `site/fedlex-aenderungen.csv`: Deutsch
- `site/fr/index.html` und `site/fr/fedlex-aenderungen.csv`: Französisch
- `site/it/index.html` und `site/it/fedlex-aenderungen.csv`: Italienisch

Die drei HTML-Seiten sind untereinander verlinkt. Titel, verantwortliche Stelle,
Spaltenüberschriften, Fedlex-Links und die Abkürzung der Amtlichen Sammlung
werden sprachgerecht ausgegeben: AS (de), RO (fr) und RU (it).

Die Ausgabe enthält pro Zeile:

1. Inkrafttretensdatum
2. SR-Nummer
3. Titel in der jeweiligen Sprache
4. Änderungsdatum (`jolux:dateDocument` des AS-Erlasses)
5. Fundstelle der Amtlichen Sammlung, zum Beispiel `AS 2026 422`,
   `RO 2026 422` oder `RU 2026 422`
6. verantwortliche Stelle

## Fachliche Auswahl

Die überwachte Auswahl steht verständlich in `config.json`. Aktuell enthalten
sind:

- der ganze Bereich SR 837 Arbeitslosenversicherung einschließlich aller
  Nummern, die mit `837.` beginnen;
- exakt SR 101, 173.110, 830.1, 830.11, 832.20, 832.202, 822.11,
  822.111, 822.112, 822.113, 822.114, 822.115, 823.11, 823.111 und 823.113;
- vom OR (SR 220) nur der Zehnte Titel über den Arbeitsvertrag, also die
  Art. 319–362.

Für das OR liefert Fedlex bei einem Impact die betroffenen Artikel-URIs. Eine
OR-Änderung wird deshalb nur ausgegeben, wenn mindestens einer dieser Artikel
zwischen 319 und 362 liegt. Andere OR-Änderungen bleiben ausgeblendet.

## Weshalb zwei Abfragen?

`queries/consolidations.sparql` bestimmt die Zeilen der Fedlex-Übersicht anhand
der tatsächlich in Kraft tretenden konsolidierten Fassungen. Das verhindert,
dass reine Impact-Ziele ohne neue Fassung, beispielsweise Aufhebungen, als
zusätzliche Zeilen erscheinen.

`queries/impacts.sparql` nutzt folgende JOLux-Kette:

```text
ConsolidationAbstract
  <- legalResourceSubdivisionIsPartOf <- Ziel-Untergliederung
  <- impactToLegalResource             <- LegalResourceImpact
  -> impactFromLegalResource           -> Quell-Untergliederung
  -> legalResourceSubdivisionIsPartOf  -> Act der Amtlichen Sammlung
```

Am `Act` werden `dateDocument`, `publicationDate`,
`sequenceInTheYearOfPublication` und `responsibilityOf` gelesen. Jahr und Nummer
der AS-Fundstelle werden zusätzlich gegen die Act-URI
`.../eli/oc/JAHR/NUMMER` geprüft. Kann eine Konsolidierung nicht vollständig
angereichert werden, schlägt der Lauf fehl. Eine unvollständige neue Seite wird
dann nicht veröffentlicht; die bisherige GitHub-Pages-Ausgabe bleibt bestehen.

Hat eine konsolidierte Fassung mehrere ändernde AS-Erlasse, wird für jede
AS-Fundstelle eine eigene Zeile ausgegeben. Das vermeidet mehrdeutige Zellen und
ergibt saubere WSW-Diffs.

## Zeitraum einstellen

Der dauerhaft überwachte Zeitraum steht in `config.json`:

```json
"start_date": "2026-08-18",
"end_date": {
  "years_from_today": 5
}
```

Das Startdatum bleibt fest und wird nur bewusst manuell geändert. Das Enddatum
wird bei jedem Lauf auf fünf Kalenderjahre nach dem aktuellen Tag gesetzt. In
der HTML-Ausgabe erscheint absichtlich kein täglich wechselndes konkretes
Enddatum, damit dieses allein keine WSW-Meldung auslöst.

Für einen einmaligen lokalen Lauf können beide Werte überschrieben werden:

```powershell
python .\monitor.py --start 2027-01-01 --end 2027-12-31 --output-dir "$env:TEMP\fedlex-monitor-2027"
```

Das separate Ausgabeverzeichnis verhindert, dass ein Probelauf die dauerhaft
überwachten Dateien unter `site` ersetzt.

Der Generator benötigt Python 3.11 oder neuer, aber keine externen Pakete.

## GitHub Pages einmalig aktivieren

Die Workflow-Datei `.github/workflows/fedlex-monitor.yml` testet und erzeugt die
Dateien täglich um 06:17 Uhr (Zeitzone Europe/Zurich) sowie auf manuellen Start.
Vor der ersten Veröffentlichung:

1. Änderungen auf `main` zu GitHub pushen.
2. Im Repository **Settings > Pages** öffnen.
3. Unter **Build and deployment > Source** den Wert **GitHub Actions** wählen.
4. Unter **Actions** den Workflow **Fedlex-Monitor veröffentlichen** einmal
   manuell starten.

Für das bestehende Repository sind diese URLs verfügbar:

- Deutsch: `https://lexalv.github.io/fedlex-monitor/`
- Französisch: `https://lexalv.github.io/fedlex-monitor/fr/`
- Italienisch: `https://lexalv.github.io/fedlex-monitor/it/`
- Deutsche CSV: `https://lexalv.github.io/fedlex-monitor/fedlex-aenderungen.csv`
- Französische CSV: `https://lexalv.github.io/fedlex-monitor/fr/fedlex-aenderungen.csv`
- Italienische CSV: `https://lexalv.github.io/fedlex-monitor/it/fedlex-aenderungen.csv`

GitHub zeigt die endgültige URL zusätzlich beim erfolgreichen Deploy-Schritt an.

### Zeitplan dauerhaft aktiv halten

GitHub kann Zeitpläne öffentlicher, längere Zeit unveränderter Repositorys nach
60 Tagen ohne Aktivität deaktivieren. Deshalb prüft derselbe tägliche Workflow
nach der Veröffentlichung die Datei `.github/monitor-heartbeat.txt`. Einmal pro
Monat wird darin der aktuelle Monat gespeichert und automatisch nach `main`
übertragen. Die Datei liegt nicht unter `site`, wird nicht veröffentlicht und
verändert deshalb keine von WebSite-Watcher überwachte Seite.

Der automatische Wartungs-Commit verwendet das kurzlebige `GITHUB_TOKEN` des
Workflows und enthält zusätzlich `[skip ci]`. Dadurch entsteht kein zweiter
Workflow-Lauf und keine Ausführungsschleife.

## Empfehlung für WebSite-Watcher

Die CSV-URL der gewünschten Sprache verwenden und eine einfache Textprüfung
ohne Browser/JavaScript einrichten. Die Dateien enthalten weder Laufzeitstempel
noch zufällige IDs. Bei unveränderten Fedlex-Daten sind wiederholte Ausgaben
byte-identisch. Sortiert wird nach Inkrafttretensdatum, natürlicher SR-Nummer
und anschließend stabil nach Änderungsdatum und Fundstelle.

## Lokaler Test

Im Ordner dieses Monitors:

```powershell
python -m unittest discover -s tests -v
python .\monitor.py
```

Der zweite Befehl sendet zwei form-urlencodierte POST-Anfragen an
`https://fedlex.data.admin.ch/sparqlendpoint`. Jede Abfrage enthält Deutsch,
Französisch und Italienisch. Die fertigen Dateien werden atomar nach `site`
geschrieben.

## Technische Quellen

- [Fedlex-JOLux: Impacts](https://swiss.github.io/fedlex-jolux/impacts.html)
- [Fedlex-JOLux: Changes](https://swiss.github.io/fedlex-jolux/changes.html)
- [Offizielles Fedlex-SPARQL-Tutorial](https://github.com/swiss/fedlex-sparql)
- [GitHub Pages mit eigenen Actions-Workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
