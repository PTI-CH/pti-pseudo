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
