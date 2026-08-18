# PTI-Pseudonymisierer

Erkennt Personendaten in Texten und Dokumenten, ersetzt sie durch Pseudonyme und
führt sie auf Wunsch wieder zurück. Damit lassen sich Unterlagen an externe
Dienste geben — etwa an eine KI — ohne dass Personendaten das Haus verlassen.

Teil der PTI-Werkzeugfamilie unter `*.pti-tip-hub.ch`, angebunden an das
gemeinsame Anmeldeverfahren des TIP-Hubs.

> **Dieses Repository enthält eine Kopie von [Presidio](https://github.com/data-privacy-stack/presidio)**
> (MIT) mit vollständiger Historie. Presidio liegt im Baum und wird als
> Editable-Install geladen — nicht als PyPI-Paket. Eine Änderung an
> `presidio-analyzer/…` wirkt darum sofort. Einzelheiten und die Regeln für
> Upstream-Merges stehen in [CLAUDE.md](CLAUDE.md).

## Was es kann

| | |
|---|---|
| **Erkennen** | Drei Ebenen: eigene Begriffsliste, Muster mit Prüfziffer (AHV, IBAN, Telefon), deutsche Namenserkennung (spaCy) |
| **Ersetzen** | Umkehrbar (`[[PERSON_1]]`, Zuordnung im verschlüsselten Tresor) oder endgültig anonym |
| **Zurückführen** | Verlustfrei — der zurückgeführte Text ist byte-identisch zum Original |
| **Formate** | DOCX (inkl. Kopf-/Fusszeilen, Tabellen, Fussnoten, Kommentare, Textboxen), TXT, CSV |

Schweizer Erkenner: AHV-Nummer mit EAN-13-Prüfziffer, CH-/LI-IBAN mit
mod-97-Prüfung, Telefonnummern mit Vorwahlprüfung, Kontrollschilder,
Postleitzahlen.

Der fachliche Entwurf (Ziele, Datenmodell, API, Phasenplan) liegt im
Schwester-Repo `landing` unter `frontend/public/ideen/pseudonymizer.html` und
ist live unter <https://pti-tip-hub.ch/ideen/pseudonymizer.html> lesbar. Dieser
Stand setzt **Phase 1** daraus um.

## Lokal starten

Voraussetzung: Python 3.11–3.14 und Node 22.

```bash
python -m venv .venv
.venv/Scripts/pip install -r pti-requirements-dev.txt     # Linux/macOS: .venv/bin/pip

# Backend (SSO abgeschaltet, SQLite im Speicher)
HUB_AUTH=off DATABASE_URL="sqlite://" VAULT_MASTER_KEY=$(openssl rand -hex 32) \
  .venv/Scripts/python -m uvicorn backend.main:app --reload --port 8000

# Frontend in einer zweiten Konsole
cd frontend && npm install && npm run dev
```

Die Oberfläche läuft dann auf <http://localhost:5173> und leitet `/api` auf den
Port 8000 weiter. `DEV_ROLE=standard` prüft den Verweigerungspfad für
Nicht-Administratoren.

Ohne `HUB_AUTH=off` verlangt jede Route eine gültige TIP-Hub-Sitzung.

## Tests

```bash
.venv/Scripts/python -m pytest backend/tests -q     # 408 Tests
.venv/Scripts/python -m ruff check backend/
cd frontend && npm run type-check
```

Zwei Dinge, die beim Testen leicht schiefgehen:

- **Echte Umlaute verwenden.** Das deutsche Modell erkennt transliterierte
  Formen schlechter und hält etwa „Spaeter" für einen Personennamen, „Später"
  dagegen nicht. Testdaten mit ae/oe/ue messen darum die falsche Grösse.
- **Keine echten Personendaten ins Repository** — auch nicht in Fixtures.
  `golden-set/` ist per `.gitignore` auf `synthetic/` beschränkt.

## Erkennungsqualitaet

Neben den Funktionstests gibt es ein Golden-Set: synthetische Dokumente, bei
denen vorher feststeht, was gefunden werden muss und was nicht.

```bash
.venv/Scripts/python -m pytest backend/tests/test_golden_set.py -v   # Gate
.venv/Scripts/python scripts/golden_set_report.py                    # Bericht
```

Messstand, Zielmarken und die bekannten Modellschwaechen stehen in
[golden-set/README.md](golden-set/README.md).

## Betrieb

Läuft auf Infomaniak jCloud unter <https://pseudo.pti-tip-hub.ch>.
Einrichtung, Deploy-Wege und Fehlersuche stehen in
[JELASTIC_SETUP.md](JELASTIC_SETUP.md), die Secrets der Pipeline in
[.github/workflows/README.md](.github/workflows/README.md).

Push auf `main` löst den Produktiv-Deploy aus.

## Sicherheit

- Dokument-Bytes werden **nicht gespeichert**. Die Verarbeitung ist ephemer.
- Der Tresor speichert die Originale AES-256-GCM-verschlüsselt mit einem je
  Mandant abgeleiteten Schlüssel; der Suchschlüssel ist ein HMAC, kein Klartext.
- Protokolliert werden nur Metadaten und Zeichenzahlen — nie Inhalte, nie
  Dateinamen.
- **`VAULT_MASTER_KEY` geht verloren = alle Zuordnungen sind unwiderruflich
  unlesbar.** Der Schlüssel gehört in den Passwortmanager, nicht nur ins
  Jelastic-Panel.

## Lizenz

Der Presidio-Anteil steht unter der MIT-Lizenz (siehe [LICENSE](LICENSE) und
[NOTICE](NOTICE)). Die PTI-Ergänzungen unter `backend/`, `frontend/`, `deploy/`
und `scripts/` sind internes Material der PTI Schweiz.
