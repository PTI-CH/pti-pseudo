# Externe API des PTI-Pseudonymisierers

Diese Datei richtet sich an Entwickler anderer PTI-Anwendungen (Translator,
PPM, HIKA, …), die Texte und Dokumente über den Maschinenpfad des
Pseudonymisierers verarbeiten wollen — dieselbe Funktionalität wie die
Oberfläche, ohne Anmeldung am TIP-Hub.

## Zugang

1. Ein Administrator erzeugt in der Oberfläche unter **„API-Schlüssel"**
   einen Schlüssel (Bezeichnung, optional ein Ablaufdatum). Der Klartext-Token
   wird **genau einmal** angezeigt — sofort sichern, es gibt keine zweite
   Anzeige.
2. Jede Anfrage trägt den Token in einem Header:

   ```
   X-Api-Key: ptps_…
   ```

   Alternativ, für Clients ohne eigenen Header-Support:

   ```
   Authorization: Bearer ptps_…
   ```

   `X-Api-Key` gewinnt, wenn beide gesetzt sind.
3. **Nur HTTPS.** Ein Aufruf über Klartext-HTTP wird zur HTTPS-Version
   umgeleitet — der Token ist dann aber schon einmal unverschlüsselt
   unterwegs gewesen. Immer `https://` verwenden.

Ein gültiger Schlüssel gilt **innerhalb des PTI-Mandanten als vollberechtigt**
(alles, was die Oberfläche kann: erkennen, ersetzen, zurückführen,
Zuordnungstabellen anlegen und lesen, Begriffsliste pflegen). Es gibt in dieser
Etappe keine Scopes pro Schlüssel — ein Schlüssel wird an eine PTI-Anwendung
ausgegeben, nicht an einen einzelnen Endnutzer.

**Eine Ausnahme: `DELETE /mappings/{id}` ist über einen API-Schlüssel nicht
erlaubt** und liefert `403`. Das Löschen einer Zuordnungstabelle ist die
einzige unumkehrbare Operation der Anwendung — danach lässt sich kein damit
pseudonymisiertes Dokument je wieder zurückführen. Ein geleakter oder
vergessener Schlüssel soll die Rückführbarkeit einer ganzen Organisation nicht
vernichten können; alles andere, was ein Schlüssel anrichtet, lässt sich
nachträglich korrigieren. Löschen darf deshalb nur ein Mensch mit CRM-Rolle
`admin` in der Oberfläche.

Jeder Aufruf über einen Schlüssel erscheint in der Nutzungserfassung als Aktor
`apikey:<Schlüssel-ID>` (nie mit Inhalten). Bewusst die ID und nicht die
Bezeichnung: Bezeichnungen sind nicht eindeutig — ein widerrufener Schlüssel
wird gern unter demselben Namen neu ausgestellt, und eine Auswertung nach
Bezeichnung schriebe dem neuen die Aufrufe des alten zu. Administratoren sehen
die Auswertung in der Oberfläche unter **API-Schlüssel → Nutzung**
(`GET /api/v1/api-keys/usage`, nicht über die externe API erreichbar).

Die Schlüsselverwaltung selbst (`POST/GET/DELETE /api/v1/api-keys`) ist
**nicht** über diese externe API erreichbar — nur über die Oberfläche mit
Admin-Rolle. Ein Schlüssel kann sich also nicht selbst weitere Schlüssel
ausstellen.

## Basis-URL

```
https://pseudo.pti-tip-hub.ch/api/ext/v1
```

Alle Pfade unten sind relativ dazu.

## Fehlerformat

Fehlende oder ungültige Authentisierung liefert `401` mit einem auswertbaren
Code:

```json
{"detail": {"code": "API_KEY_MISSING", "message": "Kein API-Schluessel. Header 'X-Api-Key' oder 'Authorization: Bearer <token>' mitschicken."}}
```

```json
{"detail": {"code": "API_KEY_INVALID", "message": "API-Schluessel ungueltig, widerrufen oder abgelaufen."}}
```

`API_KEY_INVALID` deckt bewusst drei Fälle ab (unbekannt, widerrufen,
abgelaufen) — die Antwort soll nicht verraten, ob ein Token jemals gültig war.

Ein Sonderfall der Reihenfolge: FastAPI validiert den Request-Body, **bevor**
es die Schlüsselprüfung ausführt. Ungültiges JSON ohne Schlüssel liefert
deshalb `422` und nicht `401`. Das ist keine Umgehung — verarbeitet wird ohne
gültigen Schlüssel nichts —, sondern nur eine irreführende Fehlernummer bei
einem Aufruf, der ohnehin schon kaputt ist.

Alle anderen Fehler folgen dem Standard-FastAPI-Format `{"detail": "…"}`
(String) bzw. `{"detail": [...]}` (Validierungsfehler von Pydantic):

| Code | Bedeutung |
|---|---|
| `400` | Ungültige Eingabe — unbekannter Modus, unbekannter Entitätstyp, leere Kategorienliste, ungültige Kennung |
| `403` | Aktion einem Administrator in der Oberfläche vorbehalten — über einen Schlüssel `DELETE /mappings/{id}`, ausserdem die gar nicht extern erreichbare Schlüsselverwaltung `/api/v1/api-keys` |
| `404` | Zuordnungstabelle oder Begriff nicht gefunden (auch wenn sie existiert, aber einem anderen Mandanten gehört) |
| `409` | Der Begriff steht bereits in der Liste (`POST /terms`) |
| `413` | Datei oder Text über dem Grössenlimit (siehe unten) |
| `415` | Dateiformat nicht unterstützt |
| `422` | Pydantic-Validierung (z. B. Pflichtfeld fehlt) |

Ein unbekannter Pfad unterhalb von `/api/` liefert ein auswertbares JSON-404
(`{"code": "NOT_FOUND", ...}`), nie eine HTML-Seite.

## Grenzen

- **Uploads:** maximal 25 MiB (`413`, wenn überschritten).
- **Text (JSON-Pfad):** maximal 300'000 Zeichen (`413`, wenn überschritten).
  Die Grenze gilt für alle drei Textendpunkte — `documents:detect`,
  `documents:anonymize` **und** `documents:reverse` —, damit die Aufruferin
  nur eine Zahl kennen muss.
- Kein serverseitiges Rate-Limit in dieser Etappe.

## Kategorienwahl (`entities`)

`documents:detect`, `:anonymize` und `:anonymize-file` nehmen ein optionales
Feld `entities` entgegen — die Liste der Entitätstypen, auf die sich Erkennung
und Ersetzung beschränken sollen. Im JSON-Textpfad eine Liste
(`"entities": ["PERSON", "CH_IBAN"]`), im Multipart-Formular eine
kommagetrennte Zeichenkette (`entities=PERSON,CH_IBAN`).

| `entities` | Erkennung | Ersetzung |
|---|---|---|
| fehlt / `null` | alle Katalogtypen | Default-Policy: `redact`-Typen werden ersetzt, `keep`-Typen (siehe unten) nur markiert |
| Liste | nur die genannten Typen | **alle** genannten Typen werden ersetzt — schlägt die Default-Policy, auch für `keep`-Typen |

Ein unbekannter Typ liefert `400` mit der Liste der erlaubten Typen; eine
leere Liste ebenfalls `400` („Mindestens eine Kategorie wählen.").

`keep`-Typen (`CH_POSTAL_CODE`, `CH_PLATE`) werden ohne ausdrückliche Wahl nur
markiert und im Text belassen, weil sie ohne Kontext mehrdeutig sind (eine
vierstellige Zahl kann Jahr, Betrag oder PLZ sein). Wer sie bewusst in
`entities` aufführt, bekommt sie ersetzt.

Der vollständige Katalog steht am Endpunkt `GET /entities` (siehe unten) —
das ist die massgebliche Quelle für gültige Werte, nicht diese Datei.

## Endpunkte

### `POST /documents:detect`

Findet Entitäten, ohne etwas zu ersetzen oder zu speichern.

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:detect \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{"text": "Herr Andreas Meier wohnt in 8001 Zürich."}'
```

```json
{
  "entities": [
    {
      "segment_id": "text",
      "entity_type": "PERSON",
      "label": "Person",
      "start": 5,
      "end": 18,
      "score": 0.85,
      "confidence": "gruen",
      "text": "Andreas Meier"
    }
  ],
  "char_count": 40
}
```

`confidence` ist eine Ampelstufe (`gruen` ≥ 0.85, `gelb` ≥ 0.5, sonst `rot`).

### `POST /documents:anonymize`

Pseudonymisiert einen Text. `mode` steuert, ob eine Zuordnungstabelle
entsteht:

- `reversible` (Default) — legt einen Vault an (oder verwendet `vault_id`
  weiter) und liefert `vault_id` zurück. Nur damit lässt sich der Text später
  zurückführen.
- `anonymous` — keine Zuordnungstabelle, endgültig. `vault_id` ist dann
  `null`.

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:anonymize \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{
    "text": "Herr Andreas Meier wohnt in 8001 Zürich.",
    "mode": "reversible",
    "vault_label": "Anfrage #4711",
    "entities": ["PERSON"]
  }'
```

```json
{
  "text": "Herr [[PERSON_1]] wohnt in 8001 Zürich.",
  "entities": [ { "segment_id": "text", "entity_type": "PERSON", "...": "..." } ],
  "applied": 1,
  "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "mode": "reversible",
  "char_count": 40
}
```

**`vault_id` unbedingt aufbewahren** — ohne sie lässt sich der Text nicht
zurückführen und die Zuordnungstabelle ist über die Oberfläche zwar sichtbar,
aber nicht ohne Weiteres wieder zuzuordnen.

Weitere Felder: `vault_id` (bestehenden Vault weiterverwenden statt einen
neuen anzulegen), `selected_entities` (Befund-Auswahl aus einem vorherigen
`detect`-Lauf — schlägt sowohl die Default-Policy als auch `entities`).

### `POST /documents:anonymize-file`

Pseudonymisiert ein Dokument formaterhaltend (DOCX, TXT, CSV). Multipart statt
JSON, weil eine Datei mitgeschickt wird. Die Antwort trägt das Ergebnis als
Base64, nicht als Datei-Download — daneben steht in derselben Antwort auch der
Befund.

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:anonymize-file \
  -H "X-Api-Key: ptps_…" \
  -F "file=@brief.docx" \
  -F "mode=reversible" \
  -F "vault_label=Anfrage #4711" \
  -F "entities=PERSON,CH_IBAN"
```

```json
{
  "filename": "brief.docx",
  "content_base64": "UEsDBBQAAAAIAA…",
  "entities": [ { "...": "..." } ],
  "applied": 3,
  "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "mode": "reversible",
  "char_count": 512
}
```

Die verarbeitete Datei zurückschreiben:

```bash
jq -r '.content_base64' antwort.json | base64 -d > brief-pseudonymisiert.docx
```

(`base64 --decode` auf macOS/BSD.)

### `POST /documents:reverse`

Führt einen pseudonymisierten Text verlustfrei zurück — der zurückgeführte
Text ist byte-identisch zum Original. Erzeugt wieder Klartext-Personendaten,
entsprechend vorsichtig behandeln.

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:reverse \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{
    "text": "Herr [[PERSON_1]] wohnt in 8001 Zürich.",
    "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
  }'
```

```json
{
  "text": "Herr Andreas Meier wohnt in 8001 Zürich.",
  "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
}
```

### `POST /documents:reverse-file`

Dasselbe für Dokumente — Multipart, Antwort wieder als Base64.

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:reverse-file \
  -H "X-Api-Key: ptps_…" \
  -F "file=@brief-pseudonymisiert.docx" \
  -F "vault_id=7c9e6679-7425-40de-944b-e07fc1f90ae7"
```

```json
{
  "filename": "brief-pseudonymisiert.docx",
  "content_base64": "UEsDBBQAAAAIAA…",
  "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
}
```

### Zuordnungstabellen (`/mappings`)

Eine Zuordnungstabelle (Vault) hält Original→Pseudonym-Paare eines oder
mehrerer Läufe. Die Originale sind AES-GCM-verschlüsselt; kein Endpunkt liefert
sie im Klartext — nur `documents:reverse(-file)` löst einzelne Vorkommen auf.

```bash
# Liste, neueste zuerst
curl -H "X-Api-Key: ptps_…" \
  https://pseudo.pti-tip-hub.ch/api/ext/v1/mappings
```

```json
{"mappings": [{"id": "7c9e6679-…", "label": "Anfrage #4711", "created_at": "2026-08-18T09:00:00+00:00", "expires_at": null, "entry_count": 3}]}
```

```bash
# Leere Zuordnungstabelle anlegen (normalerweise entsteht sie automatisch bei anonymize)
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/mappings \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{"label": "Anfrage #4711"}'
```

```bash
# Einträge einer Zuordnungstabelle (ohne Klartext-Originale)
curl -H "X-Api-Key: ptps_…" \
  https://pseudo.pti-tip-hub.ch/api/ext/v1/mappings/7c9e6679-7425-40de-944b-e07fc1f90ae7

# Optional auf einen Entitätstyp einschränken
curl -H "X-Api-Key: ptps_…" \
  "https://pseudo.pti-tip-hub.ch/api/ext/v1/mappings/7c9e6679-…?entity_type=PERSON"
```

```json
{
  "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "label": "Anfrage #4711",
  "created_at": "2026-08-18T09:00:00+00:00",
  "entries": [
    {"id": "…", "entity_type": "PERSON", "label": "Person", "pseudonym": "[[PERSON_1]]", "created_at": "2026-08-18T09:00:00+00:00"}
  ]
}
```

Der Query-Parameter `entity_type` filtert die zurückgelieferten Einträge auf
genau einen Typ (Werte wie in `entities`, siehe `GET /entities`). Ohne ihn
kommen alle Einträge.

**Löschen geht nicht über die API.** `DELETE /mappings/{id}` weist einen
Aufruf mit API-Schlüssel mit `403` ab:

```json
{"detail": "Zuordnungstabellen loeschen ist nur Administratoren in der Oberflaeche vorbehalten — nicht ueber API-Schluessel."}
```

Das Löschen ist unumkehrbar (siehe „Zugang“ oben) und deshalb einem
Administrator in der Oberfläche vorbehalten. Wer eine Zuordnungstabelle
loswerden will, lässt sie dort löschen — oder arbeitet von vornherein mit
`"mode": "anonymous"`, dann entsteht gar keine.

### Begriffsliste (`/terms`)

Eigene Begriffe (Projektnamen, interne Bezeichnungen), die immer erkannt und
ersetzt werden — unabhängig von jedem statistischen Erkenner. Die Liste ist
mandantenweit; wer hier schreibt, ändert das Verhalten für alle Aufrufer.

```bash
curl -H "X-Api-Key: ptps_…" \
  https://pseudo.pti-tip-hub.ch/api/ext/v1/terms
```

```json
{
  "terms": [
    {"id": "…", "term": "Projekt Nordwind", "entity_type": "ORGANIZATION", "label": "Organisation", "match_mode": "normalized", "created_at": "2026-08-18T09:00:00+00:00"}
  ],
  "entity_types": [{"value": "PERSON", "label": "Person"}, {"value": "...", "label": "..."}]
}
```

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/terms \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{"term": "Projekt Nordwind", "entity_type": "ORGANIZATION", "match_mode": "normalized"}'
```

`match_mode`: `exact` (achtet auf Gross-/Kleinschreibung) oder `normalized`
(gleicht Umlautschreibweisen und Leerraum an, Default).

```bash
curl -X DELETE -H "X-Api-Key: ptps_…" \
  https://pseudo.pti-tip-hub.ch/api/ext/v1/terms/<id>
```

### Entitätskatalog (`GET /entities`)

Der vollständige Katalog aller Entitätstypen — die massgebliche Quelle für
gültige Werte in `entities`.

```bash
curl -H "X-Api-Key: ptps_…" \
  https://pseudo.pti-tip-hub.ch/api/ext/v1/entities
```

```json
{"entities": [
  {"entity_type": "PERSON", "label": "Person", "description": "Namen natürlicher Personen (spaCy-NER, deutsches Modell).", "default_policy": "redact", "source": "recognizer"},
  {"entity_type": "CH_POSTAL_CODE", "label": "Postleitzahl (CH)", "description": "Vierstellige CH-PLZ — nur mit Adresskontext erkannt, mehrdeutig.", "default_policy": "keep", "source": "recognizer"},
  {"entity_type": "PTI_PROJEKT", "label": "Projekt (PTI)", "description": "Interne PTI-Projektbezeichnung — nur über die Begriffsliste erkannt, kein Recognizer findet sie von selbst.", "default_policy": "redact", "source": "custom_terms"}
]}
```

Vollständige Typenliste (Stand dieser Etappe):

| `entity_type` | Label | Default-Policy | Quelle |
|---|---|---|---|
| `PERSON` | Person | `redact` | Erkenner |
| `LOCATION` | Ort | `redact` | Erkenner |
| `ORGANIZATION` | Organisation | `redact` | Erkenner |
| `EMAIL_ADDRESS` | E-Mail-Adresse | `redact` | Erkenner |
| `IBAN_CODE` | IBAN (generisch) | `redact` | Erkenner |
| `CREDIT_CARD` | Kreditkartennummer | `redact` | Erkenner |
| `IP_ADDRESS` | IP-Adresse | `redact` | Erkenner |
| `URL` | Web-Adresse | `redact` | Erkenner |
| `DATE_TIME` | Datum/Zeit | `redact` | Erkenner |
| `CH_AHV` | AHV-Nummer | `redact` | Erkenner |
| `CH_IBAN` | IBAN (CH/LI) | `redact` | Erkenner |
| `CH_PHONE` | Telefonnummer (CH) | `redact` | Erkenner |
| `CH_POSTAL_CODE` | Postleitzahl (CH) | `keep` | Erkenner |
| `CH_PLATE` | Kontrollschild (CH) | `keep` | Erkenner |
| `PTI_PROJEKT` | Projekt (PTI) | `redact` | Begriffsliste |
| `PTI_PRODUKT` | Produkt (PTI) | `redact` | Begriffsliste |

`source: custom_terms` heisst: kein Erkenner findet diesen Typ von selbst —
er entsteht ausschliesslich über einen Eintrag in `/terms`.

## Beispiel-Ablauf: anonymisieren → extern verarbeiten → zurückführen

```bash
# 1. Anonymisieren, vault_id sichern
ANTWORT=$(curl -s -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:anonymize \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{"text": "Herr Andreas Meier ruft an.", "mode": "reversible"}')
TEXT=$(echo "$ANTWORT" | jq -r .text)
VAULT_ID=$(echo "$ANTWORT" | jq -r .vault_id)

# 2. Den pseudonymisierten Text extern verarbeiten (z. B. an eine KI schicken)
# ... eigene Verarbeitung von $TEXT ...
VERARBEITET="$TEXT"   # Platzhalter fuer das eigene Ergebnis

# 3. Zurückführen
curl -s -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:reverse \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d "{\"text\": $(echo "$VERARBEITET" | jq -Rs .), \"vault_id\": \"$VAULT_ID\"}"
```

Wichtig: Schritt 2 darf den Pseudonym-Platzhalter (`[[PERSON_1]]`) nicht
verändern — nur exakt erhaltene Platzhalter lassen sich in Schritt 3 wieder
auflösen.

## Interaktive Dokumentation

`GET /api/docs` liefert die vollständige OpenAPI-Oberfläche — inklusive des
Maschinenpfads mit ausgewiesener Authentisierung. Sie liegt **hinter dem
TIP-Hub-SSO-Gate** (Menschenpfad), ist also nur für angemeldete Entwickler
erreichbar, nicht über einen API-Schlüssel.
