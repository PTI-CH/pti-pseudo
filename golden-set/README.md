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

## Die KI-Schicht wird standardmaessig NICHT mitgemessen

Seit Phase 3 gibt es eine KI-Pruefung der Namensgrenzen (`ki_pruefung`). Der
Golden-Set-Lauf schaltet sie **nicht** zu, und das aus zwei Gruenden:

1. **Vergleichbarkeit.** Alle Zahlen seit dem 2026-08-18 sind ohne KI
   gemessen. Ein stiller Wechsel machte die Reihe wertlos — man wuesste nicht
   mehr, ob eine Verbesserung von der Engine oder vom Dienst kommt.
2. **Kosten und Verfuegbarkeit.** Jeder Testlauf verursachte sonst echte
   Anfragen und haenge von einem fremden Dienst ab. Ein Gate, das rot wird,
   weil Infomaniak gerade wartet, ist kein Gate.

Ein Lauf **mit** KI wird ausdruecklich angefordert und misst gegen den echten
Dienst. Beide Zahlen gehoeren hierher, nebeneinander — die Differenz ist das,
was die Schicht tatsaechlich beitraegt.

### Messung der KI-Schicht am echten Dienst, 2026-08-24

Das ganze Golden-Set, einmal ohne und einmal mit KI-Pruefung, gegen
`swiss-ai/Apertus-v1.5-70B`:

| | ohne KI | mit KI |
|---|---|---|
| **PERSON** Precision / Recall | 95.0 % / 95.0 % | **100 % / 100 %** |
| Gesamt-Precision | 90.8 % | **92.3 %** |
| Gesamt-Recall | 96.7 % | **98.4 %** |

`Sandra Zbinden` wird vollstaendig gefunden, der Fehlalarm auf `Sandra`
verschwindet — das Abnahmekriterium ist erfuellt. Der einzige verbleibende
verfehlte Fund ist `Meldestelle fuer Geldwaescherei`, eine ORGANIZATION in
einem Dokument ohne Begriffsliste; die liegt ausserhalb dieser Schicht.

**Neun von neun Anfragen gelangen**, jede in 0.7 bis 2.2 Sekunden. Keine
Wiederholung war noetig.

### Der Umweg, der dabei sichtbar wurde

Mit dem Modell `mistralai/Ministral-3-14B-Instruct-2512` scheiterten drei von
neun Anfragen **reproduzierbar**. Es sah zuerst nach einem Zeitproblem aus:
Die Aufrufe brachen an der Zeitgrenze ab. Tatsaechlich gab das Modell bei
diesen Eingaben eine oeffnende Klammer aus und danach 2000 Token Leerzeichen
(`finish_reason: length`) — die Antwort war damit kein gueltiges JSON.

Weder eine hoehere Temperatur noch ein strikteres Schema noch ein zweiter
Versuch halfen:

| Modell | Temperatur | brauchbar |
|---|---|---|
| Ministral-3 | 0.0 | 0/3 |
| Ministral-3 | 0.2 | 0/3 |
| **Apertus** | 0.0 | **3/3** |
| gemma-4 | 0.0 | 3/3 |

Ministral-3 ist fuer Massenklassifikation das empfohlene Modell und war
zwischenzeitlich gesetzt. **An dieser Aufgabe gemessen faellt es aus.** Die
Wahl steht jetzt auf Apertus, und sie ist eine Messung und keine Uebernahme.

Zwei Lehren, die ueber diesen Fall hinausgehen: Eine knappe Zeitgrenze
verwandelt fremde Fehler in scheinbar eigene. Und eine Wiederholung hilft
gegen Aussetzer, nie gegen reproduzierbares Verhalten.

### Der Zielfall einzeln



Der Fusszeilentext aus `tabelle_kopf_fusszeile`, die Stelle mit dem
Namenssplit:

```
Text:    Diese Fallakte wird von Sandra Zbinden bearbeitet, Rueckfragen an
         sandra.zbinden@example.ch.
OHNE KI: PERSON ['Sandra']
MIT KI:  PERSON ['Sandra Zbinden']
verworfen: 0
```

Ein Fenster, ein Aufruf, korrekte Erweiterung. Das Schema hat gehalten.

Was das **noch nicht** zeigt: wie sich das Modell auf schwierigen Faellen
verhaelt (mehrere Namen im selben Fenster, Namen an Zellgrenzen, franzoesische
Namen) und ab welcher Fensterzahl gestueckelt werden muss. Beides gehoert
gemessen, bevor die Schicht als verlaesslich gilt.

**Abnahmekriterium der Etappe:** `tabelle_kopf_fusszeile` findet
`Sandra Zbinden` vollstaendig, und der Fehlalarm auf `Sandra` verschwindet.
Ohne diesen Nachweis hat die Etappe ihr Ziel verfehlt, unabhaengig davon, was
die Gesamtzahl sagt.

---

## Messstand vom 2026-08-24 (abends) — Gate wieder gruen

Nach der Wortart-Regel (`backend/engine/wortarten.py`), die den Befund des
vorigen Abschnitts aufloest:

| | vorher | nachher | Marke |
|---|---|---|---|
| Gesamt-Precision | 89.4 % | **90.8 %** | ≥ 90 % — gehalten |
| Gesamt-Recall | 96.7 % | **96.7 %** | — |
| PERSON | 90.5 % / 95.0 % | **95.0 % / 95.0 %** | ≥ 90 % Recall — gehalten |
| Struktur-PII | 100 % | 100 % | 100 % — gehalten |

**Die Marke wurde nicht gesenkt, sondern die Erkennung verbessert.** Ein
einzelnes Token, das dasselbe Modell als ``NOUN`` und nicht als ``PROPN``
fuehrt, ist kein Personenname. `Lagebericht` verschwindet damit aus den
Fehlalarmen — und mit ihm der laengst dokumentierte Fehlalarm auf `Spaeter`
in transliterierter Schreibweise.

### Der Zwischenschritt, der widerlegt wurde

Die erste Fassung der Regel galt fuer alle drei NER-Typen. Gemessen:

| | Precision | Recall |
|---|---|---|
| alle drei Typen | 98.1 % | **85.2 %** |
| nur `PERSON` | 90.8 % | 96.7 % |

Sie kostete `Talblick` (eine Firma, sechsmal) und `Bahnhofstrasse`. Der Grund
ist Sprache, nicht Zufall: Organisations- und Ortsnamen *sind* im Deutschen
ueberwiegend aus gewoehnlichen Woertern gebaut. Sieben echte Funde gegen zwei
Fehlalarme waere in diesem Werkzeug ein schlechtes Geschaeft — die Regel gilt
deshalb nur fuer `PERSON`.

Das ist genau der Zweck dieses Verzeichnisses: Die naheliegende, breite Regel
sah gut aus und war falsch, und ohne Messung waere das erst im Betrieb
aufgefallen.

### Was weiterhin offen ist

Drei Fehlalarme bleiben und sind alle vom Typ „Sachbegriff als Entitaet",
diesmal als ORGANIZATION und LOCATION: `Kontrollschild`, `Fallnummer`
(zweimal), `Intern`, `Polizei-Abfrageplattform`. Sie zu erfassen erfordert
etwas anderes als die Wortart — der naechste Ansatz waere der Satzkontext,
also Phase 3.

---

## Messstand vom 2026-08-24 (nachmittags) — GATE ROT (aufgeloest)

Nach den Dokumenten `xlsx_personenliste` und `pptx_notizen`, die den neuen
XLSX- und PPTX-Adapter durch die ganze Engine fuehren. 10 → 12 Dokumente,
54 → 61 erwartete Funde:

| | vorher | nachher | Marke |
|---|---|---|---|
| Gesamt-Precision | 92.9 % | **89.4 %** | ≥ 90 % — **gerissen** |
| Gesamt-Recall | 96.3 % | **96.7 %** | — |
| PERSON-Recall | 93.8 % | **95.0 %** | ≥ 90 % — gehalten |
| Struktur-PII | 100 % | 100 % | 100 % — gehalten |

**Die Marke wird nach Hausregel nicht nachgezogen.** Der Satz ist um zwei
schwierige Dokumente gewachsen, und die Messung sagt etwas Wahres: Die
Erkennung ist auf Tabellen und Folien schlechter als auf Fliesstext.

### Warum, und warum das kein Zufall ist

Drei neue Fehlalarme, alle vom selben Typ:

```
[xlsx_personenliste] LOCATION 'Fallnummer'
[pptx_notizen]       PERSON   'Lagebericht'
[pptx_notizen]       ORGANIZATION 'Intern'
```

Das sind einzelne, grossgeschriebene Substantive ohne Satzkontext — Schwaeche 3
(„Sachbegriffe als Entitaeten"), dieselbe wie bei `Kontrollschild` und
`Fallnummer`. Neu ist nicht die Schwaeche, sondern **wie stark sie zu Buche
schlaegt**: Ein Folientitel ist ein Wort. Eine Tabellenueberschrift ist ein
Wort. Fliesstext liefert dem Modell einen Satz, eine Praesentation nicht.

Die Formate, die dieser Stand neu unterstuetzt, sind also genau die, auf denen
das Modell am schwaechsten ist. Das ist eine Aussage ueber die Engine, nicht
ueber die Adapter — die Adapter liefern den Text vollstaendig und korrekt ab,
nachgewiesen in `backend/tests/test_adapter_xlsx.py` und `…_pptx.py`.

### Der naheliegende Ansatz

Alle drei Fehlalarme sind **einzelne Tokens**, die das Modell selbst als
Nomen (nicht als Eigenname) taggt. Eine Regel „ein alleinstehendes Token,
das spaCy als `NOUN` und nicht als `PROPN` fuehrt, ist kein PERSON/LOCATION/
ORGANIZATION" wuerde alle drei treffen. Das ist Engine-Arbeit mit eigenem
Risiko — spaCy taggt auch echte Namen gelegentlich als `NOUN`, und dann
verschwaende die Regel Recall, also genau das Gut, das hier zaehlt. Sie
gehoert in einen eigenen Durchgang mit diesem Satz als Waechter.

### Ein Befund ueber die Segmentgroesse

`Zwicky\tMarlene` steht als **erwarteter** Fund im Satz: In der Tabelle stehen
Nach- und Vorname in getrennten Zellen, und das Modell liest sie ueber den
Tabulator hinweg als einen Namen. Das ist der Beleg, dass die Zeile die
richtige Segmentgroesse ist — bei einem Segment je Zelle stuenden dort zwei
kontextlose Einzelwoerter, und genau die uebersieht das Modell.

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
