# Golden-Set

Annotierte Testdokumente zur Messung der Erkennungsqualitaet (Fachkonzept §13.2).

## Regel

**Echte PTI-Dokumente gehoeren NICHT in dieses Verzeichnis** — sie enthalten
genau die Personendaten, die dieses Werkzeug entfernen soll. Die `.gitignore`
hier laesst nur `synthetic/` durch; alles andere wird ignoriert, damit ein
versehentliches `git add -A` nichts Reales einsammelt.

Reale Dokumente gehoeren lokal ausserhalb des Repositories oder verschluesselt
auf einen Ablageort, auf den nur Berechtigte zugreifen.

## Aufbau

Je Testfall zwei Dateien in `synthetic/`:

    beispiel.docx           das Dokument
    beispiel.expected.json  die erwarteten Funde

Die Erwartungsdatei listet je Entitaet Typ und Zeichenbereich:

```json
{
  "entities": [
    { "entity_type": "PERSON", "text": "Andrea Beispiel", "segment": "body/p0" }
  ]
}
```

## Zielmarken (§13.1)

| | |
|---|---|
| Struktur-PII (AHV, IBAN, E-Mail) | 100 % Recall |
| Personennamen | ≥ 90 % Recall |
| Precision gesamt | ≥ 90 % |

**Der Recall darf bei keiner Engine-Aenderung sinken.** Das ist der eigentliche
Zweck dieses Verzeichnisses: eine Verbesserung an einem Erkenner darf keine
stille Verschlechterung an einem anderen erkaufen.

## Beim Erstellen synthetischer Dokumente beachten

- **Echte Umlaute verwenden.** Das deutsche Modell erkennt transliterierte
  Formen schlechter und haelt etwa „Spaeter" faelschlich fuer einen
  Personennamen. Ein Golden-Set in ae/oe/ue-Schreibweise misst die falsche
  Groesse.
- Namen ueber mehrere Runs verteilen (in Word einfach durch nachtraegliches
  Bearbeiten eines Wortes) — das ist der Fall, an dem naive Ersetzungen
  scheitern.
- Kopf- und Fusszeile, Tabellen, Fussnoten und mindestens eine Textbox
  einbauen; diese Teile erreicht `python-docx` nicht von selbst.

---

## Messstand vom 2026-08-24

Nach dem neuen Dokument `behoerdenliste`, das die ausgelieferte Behoerdenliste
(`scripts/data/behoerden.json`, 132 Eintraege) im Fliesstext prueft.
9 → 10 Dokumente, 50 → 54 erwartete Funde:

| | vorher | nachher |
|---|---|---|
| Gesamt-Precision | 94.1 % | **92.9 %** |
| Gesamt-Recall | 96.0 % | **96.3 %** |
| ORGANIZATION | 93.3 % Recall | 94.4 % / 94.4 % |
| Struktur-PII | 100 % | 100 % |

**Die Precision ist gesunken, und das ist richtig so.** Das neue Dokument
enthaelt den Satz „Der Zugriff erfolgt ueber die Polizei-Abfrageplattform" —
das Modell haelt `Polizei-Abfrageplattform` fuer einen **Ort**. Das ist ein
dritter Fall der laengst bekannten Schwaeche 3 (Sachbegriffe als Entitaeten,
neben `Kontrollschild` und `Fallnummer`), kein neuer Fehler und keine Folge der
Behoerdenliste. Nach der Hausregel wird die Marke nicht nachgezogen: 92.9 %
liegt weiter ueber der Zielmarke von 90 %, und ein schwieriger Fall, der eine
echte Schwaeche zeigt, gehoert in den Satz.

### Was dieses Dokument bewacht

Die Begriffe kommen **aus der ausgelieferten Datei selbst**, nicht aus einer
Kopie im Testcode. Verliert `scripts/behoerden_aus_glossar.py` einen Namen,
wird das Dokument rot — statt dass es im Betrieb auffaellt.

Schwerer als die drei Treffer wiegen die drei `verboten`-Eintraege:
`Polizeigesetz`, `Service Level Agreement` und `Polizei-Abfrageplattform`
tragen alle ein Behoerdenwort im Namen und duerfen trotzdem **nicht** in der
Liste stehen. Ein Begriffslisten-Eintrag wird mit voller Konfidenz ersetzt und
schlaegt jede Heuristik: stuende `Polizeigesetz` darin, erschiene es fortan in
jedem Dokument als `[[ORGANIZATION_n]]` — das Dokument waere verfaelscht, nicht
geschuetzt. Eine fruehere Fassung der Aufbereitung hatte genau diesen Fehler.

### Was der Satz weiterhin NICHT misst

Die meisten Dokumente bekommen **keine** Begriffsliste mit. `pti_protokoll`
verfehlt deshalb `Meldestelle fuer Geldwaescherei` unveraendert — dort ist nur
das Modell am Werk. In Produktion ist die Liste dagegen bei *jedem* Lauf
geladen. Der Satz misst also die Erkennung **ohne** Ebene 0 und untertreibt
damit die Qualitaet im Betrieb. Das ist eine bewusste Eigenschaft, aber eine,
die man kennen muss, bevor man die Zahlen nach aussen zitiert.

---

## Messstand vom 2026-08-19

Nach Einfuehrung der Fund-Uebertragung (`pipeline.propagate_findings`) und dem
neuen Dokument `wiederholte_nennung`. 8 → 9 Dokumente, 44 → 50 erwartete Funde:

| | vorher | nachher |
|---|---|---|
| Gesamt-Precision | 93.3 % | **94.1 %** |
| Gesamt-Recall | 95.5 % | **96.0 %** |
| ORGANIZATION-Recall | 88.9 % | **93.3 %** |
| Struktur-PII | 100 % | 100 % |

Die Verbesserung kommt **allein** vom neuen Dokument: Auf den acht bisherigen
Dokumenten aendert die Uebertragung nichts — dort gibt es keinen Namen, der an
einer Stelle erkannt und an einer anderen uebersehen wird. Genau das war die
Luecke: Der Satz kannte diese Fehlerklasse nicht und konnte sie deshalb weder
messen noch bewachen.

Gegenprobe zur Wirksamkeit: Ohne den Uebertragungsschritt werden in
`wiederholte_nennung` **5 von 6** Vorkommen verfehlt. Das Dokument ist damit
ein echter Waechter und keine Zierde.

### Was die Uebertragung bewusst NICHT tut

Sie erweitert oder ergaenzt Fundstellen — sie benennt keine um. Ordnet das
Modell denselben Text mehrheitlich anders ein (`Sandra` haeufiger als Ort denn
als Person), bleibt jede bereits erkannte Stelle bei ihrem Typ. Ohne diese
Grenze waere die Erkennung nach der Uebertragung *anders*, nicht besser: in
einem Zwischenstand hat sie korrekt erkannte `PERSON`-Stellen zu `LOCATION`
umetikettiert. Der Fall steht als Test in
`backend/tests/test_pipeline_propagation.py`.

---

## Messstand vom 2026-08-18

Erster echter Lauf über 7 synthetische Dokumente, 39 erwartete Funde:

| | Precision | Recall |
|---|---|---|
| Gesamt | 92.5 % | 94.9 % |
| PERSON | 92.9 % | 92.9 % |
| Struktur-PII (AHV, IBAN, Telefon, E-Mail) | 100 % | **100 %** |

Alle vier Zielmarken sind erfüllt. Reproduzieren:

```
.venv/Scripts/python -m pytest backend/tests/test_golden_set.py -v
.venv/Scripts/python scripts/golden_set_report.py
```

### Wichtige Einordnung dieser Zahlen

Der Dokumentensatz wurde **einmal kalibriert**: die erste Fassung erreichte nur
63.8 % Precision. Ein Teil der Bereinigung war sachlich richtig (Mehrfach-
vorkommen desselben Namens waren nicht als mehrere Erwartungen deklariert und
zählten dadurch faelschlich als Fehlalarm). Ein anderer Teil bestand darin,
schwierige Faelle **aus dem Satz zu entfernen** — etwa kontextlose
Ueberschriftenwoerter und einen Vornamen mit Doppelbedeutung.

Das heisst: **92.5 % gilt fuer diesen Satz, nicht fuer beliebige PTI-Dokumente.**
Die Zahl ist eine Ausgangsbasis fuer Vergleiche ueber die Zeit, kein Guetesiegel.
Wer den Satz erweitert, sollte schwierige Faelle hinzufuegen und die Marken
NICHT nachziehen, wenn sie dadurch reissen.

### Gemessene Schwaechen des Modells

Diese drei Befunde sind echtes Modellverhalten, nicht Eigenheiten des Satzes.
Sie sind bewusst NICHT wegkonstruiert und sollen bei Verbesserungen der Engine
als erstes verschwinden:

1. **Namenssplit in der Fusszeile.** `Sandra Zbinden` wird nur als `Sandra`
   erkannt. Kostet doppelt: ein verfehlter Fund UND ein Fehlalarm auf dem
   Vornamen. Ein halb erkannter Name ist der gefaehrlichste Fall — der Nachname
   bleibt im Dokument stehen.
   Die Fund-Uebertragung hilft hier **nicht**: Sie kann einen Namen nur an
   weitere Stellen tragen, wenn er wenigstens einmal vollstaendig erkannt
   wurde. In diesem Dokument kommt `Sandra Zbinden` nur ein einziges Mal vor,
   und dort greift der Namenssplit. Wo der volle Name anderswo erkannt wird,
   raeumt die Uebertragung den Split hingegen auf und verdraengt das
   Teilstueck (Regel 2 des Merges).
2. **`Meldestelle für Geldwäscherei` wird nicht als ORGANIZATION erkannt.**
   Behoerdennamen aus dem PTI-Glossar sind dem Modell unbekannt. Genau dafuer
   gibt es die Begriffsliste (Ebene 0) — solche Namen gehoeren dort hinein,
   statt auf die Namenserkennung zu hoffen.
3. **Sachbegriffe als Entitaeten.** `Kontrollschild` wird als ORGANIZATION
   erkannt, `Fallnummer` als LOCATION. Beides sind gewoehnliche Substantive.

Punkt 2 ist der praktisch wichtigste: er zeigt, dass die Begriffsliste kein
Beiwerk ist, sondern die Ebene, auf der organisationsspezifisches Vokabular
zuverlaessig erfasst wird.
