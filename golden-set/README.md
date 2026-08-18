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
2. **`Meldestelle für Geldwäscherei` wird nicht als ORGANIZATION erkannt.**
   Behoerdennamen aus dem PTI-Glossar sind dem Modell unbekannt. Genau dafuer
   gibt es die Begriffsliste (Ebene 0) — solche Namen gehoeren dort hinein,
   statt auf die Namenserkennung zu hoffen.
3. **Sachbegriffe als Entitaeten.** `Kontrollschild` wird als ORGANIZATION
   erkannt, `Fallnummer` als LOCATION. Beides sind gewoehnliche Substantive.

Punkt 2 ist der praktisch wichtigste: er zeigt, dass die Begriffsliste kein
Beiwerk ist, sondern die Ebene, auf der organisationsspezifisches Vokabular
zuverlaessig erfasst wird.
