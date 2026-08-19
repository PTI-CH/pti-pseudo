# Design: Kategorienwahl im GUI und externe API mit Schlüsseln

Datum: 2026-08-18 · Status: umgesetzt in dieser Etappe · Ergänzt Phase 1 (§12
des Fachkonzepts) um zwei Funktionen.

## 1. Ziel

1. **Kategorienwahl** — In der Oberfläche bestimmt der Mensch vor dem Lauf,
   *welche* Arten von Personendaten pseudonymisiert werden (nur IBANs, nur
   Namen, nur Telefonnummern, beliebige Kombination).
2. **Externe API** — Andere PTI-Anwendungen (Translator, PPM, HIKA, …) können
   Texte und Dokumente anonymisieren, pseudonymisieren und zurückführen —
   alles, was die Oberfläche kann — geschützt durch generierte Schlüssel, die
   ein Administrator in der Oberfläche anlegt und widerruft.

## 2. Vorbild und Abweichung

Der `pti-translator` schützt `POST /api/v1/translate` mit einem statischen
`X-Api-Key` aus der Umgebung, prüft ihn per SHA-256 + zeitkonstantem Vergleich,
trennt Maschinen- und Menschenpfad über das Routen-Präfix (SSO-Gate mit
`exempt`) und schreibt den Aufrufer als Aktor in die Nutzungserfassung.

Übernommen: Header `X-Api-Key`, Trennung per Präfix, Hash statt Klartext,
Aktor-Attribution, JSON-Fehler statt Redirect auf dem Maschinenpfad.
Abgewichen: Schlüssel liegen **in der Datenbank** (gehasht), nicht in der
Umgebung — weil hier *mehrere* Anwendungen *ohne Dashboard-Restart* Schlüssel
erhalten und wieder verlieren sollen. Verwaltung über eine Admin-Ansicht.

## 3. Feature 1 — Kategorienwahl

### 3.1 API-Vertrag

Alle Erkennungs-/Ersetzungs-Endpunkte (`documents:detect`, `:anonymize`,
`:anonymize-file`) nehmen ein optionales Feld **`entities`** entgegen:

- JSON: `"entities": ["PERSON", "CH_IBAN"]`
- Multipart: Formularfeld `entities` als kommagetrennte Liste
  (`entities=PERSON,CH_IBAN`).

Semantik:

| `entities` | Erkennung | Ersetzung (ohne `selected_entities`) |
|---|---|---|
| fehlt / `null` | alle Katalogtypen | Default-Policy: `REDACT`-Typen ersetzen, `KEEP`-Typen (PLZ, Kontrollschild) nur markieren — wie bisher |
| Liste | nur die genannten Typen | **alle** genannten Typen ersetzen — eine ausdrückliche Wahl schlägt die Default-Policy |

Unbekannte Typen → `400` mit der Liste der erlaubten Typen. Leere Liste → `400`
(„Mindestens eine Kategorie wählen."). `selected_entities` (Befund-Auswahl)
wirkt unverändert und schlägt beides.

Neuer Endpunkt **`GET /api/v1/entities`** (und `/api/ext/v1/entities`): der
Katalog aus `backend/engine/entities.py` — je Typ `entity_type`, `label`,
`description`, `default_policy`, `source` (`recognizer` | `custom_terms`).
Er ist die einzige Quelle für die Checkbox-Liste; das Frontend hält keine
Kopie mehr, die auseinanderlaufen kann.

### 3.2 Oberfläche

Im Bereich „Pseudonymisieren", zwischen Eingabe und Modus, ein Abschnitt
**„Kategorien"** mit Checkboxen (Label + Kurzbeschreibung als Tooltip):

- Vorbelegt: alle Typen mit Default-Policy `REDACT` angehakt, `KEEP`-Typen
  abgehakt mit Hinweis „mehrdeutig — standardmässig nicht ausgewählt".
- Schnellwahl „Alle" / „Keine".
- Die Auswahl wird **immer explizit** mitgeschickt. Bei der **Ersetzung** ist
  das Standardverhalten damit identisch mit heute (KEEP-Typen bleiben
  unangetastet), und wer PLZ bewusst anhakt, bekommt sie ersetzt.
- Bei der **Erkennung** ist es das nicht: eine explizite `entities`-Liste
  begrenzt auch, wonach überhaupt gesucht wird. KEEP-Typen erscheinen mit der
  Vorbelegung deshalb nicht mehr in der Befundliste — vorher wurden sie
  gefunden und markiert, jetzt gar nicht erst gesucht. Bewusst so: die
  Kategorienwahl soll bedeuten, was sie sagt. Wer die PLZ als Markierung sehen
  will, hakt sie an.
- Auswahl bleibt über Läufe hinweg erhalten (State im Panel; kein Persistieren).

### 3.3 Mitbehobener Fehler im Dateipfad

`frontend/src/core/api.ts` schickt Dateien an `documents:anonymize` /
`documents:reverse` (JSON-Routen) und erwartet `download_url`; das Backend hat
`:anonymize-file` / `:reverse-file` und liefert `content_base64`. Der
Dateipfad der Oberfläche funktioniert heute nicht. Fix: richtige Routen,
Base64 → Blob-URL für den Download.

## 4. Feature 2 — Externe API mit Schlüsseln

### 4.1 Routen

Zweiter Einhängepunkt derselben Router unter **`/api/ext/v1/`**:

```
POST /api/ext/v1/documents:detect
POST /api/ext/v1/documents:anonymize
POST /api/ext/v1/documents:anonymize-file
POST /api/ext/v1/documents:reverse
POST /api/ext/v1/documents:reverse-file
GET/POST/GET{id}/DELETE{id} /api/ext/v1/mappings
GET/POST/DELETE{id}          /api/ext/v1/terms
GET  /api/ext/v1/entities
```

Gleiche Handler, gleiche Verträge wie `/api/v1/` — die Router werden ohne
Präfix definiert und zweimal eingehängt. `/api/v1/` bleibt hinter dem
SSO-Gate; `/api/ext/` ist im Gate ausgenommen und trägt stattdessen die
Router-Dependency `require_api_key`. Schlüsselverwaltung selbst
(`/api/v1/api-keys`) gibt es **nur** über den SSO-Pfad — ein Schlüssel kann
keine weiteren Schlüssel erzeugen.

### 4.2 Authentisierung

- Header `X-Api-Key: <token>`; alternativ `Authorization: Bearer <token>`.
- Token-Format: `ptps_` + 43 Zeichen `secrets.token_urlsafe(32)`. Das Präfix
  macht den Token in Logs/Secret-Scannern erkennbar; die ersten 12 Zeichen
  (`ptps_XXXXXXX`) werden als `key_prefix` gespeichert und in der Liste
  angezeigt.
- Gespeichert wird **nur** `sha256(token)` (Hex). Der Klartext wird genau
  einmal in der Antwort auf `POST /api/v1/api-keys` gezeigt. Hohe Entropie
  macht ein langsames Hash-Verfahren unnötig; der Lookup läuft über den
  Unique-Index auf dem Hash, der Vergleich zusätzlich mit `hmac.compare_digest`.
- Fehler als JSON: fehlender Header → `401 {"code":"API_KEY_MISSING"}`,
  ungültig/widerrufen/abgelaufen → `401 {"code":"API_KEY_INVALID"}`.
  Nie ein Redirect.
- Bei Erfolg: `request.state.api_key = <ApiKey>`; `last_used_at` wird gesetzt
  (höchstens einmal pro Minute geschrieben, sonst wäre jeder Aufruf ein
  Update).

### 4.3 Datenmodell

Neue Tabelle `api_keys` (nur neue Tabelle — `init_db`/`create_all` legt sie
an, keine Migration bestehender Tabellen nötig):

| Spalte | Typ | Bedeutung |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK tenants | Phase 1: immer der PTI-Mandant |
| `label` | String(200) | Name der Anwendung, z. B. „Translator" |
| `key_prefix` | String(16) | zur Wiedererkennung in der Liste |
| `key_hash` | String(64) unique | SHA-256 Hex |
| `created_by` | String(150) | Hub-Username des Admins |
| `created_at` | timestamptz | |
| `expires_at` | timestamptz nullable | optional |
| `last_used_at` | timestamptz nullable | |
| `revoked_at` | timestamptz nullable | Widerruf ist Soft-Delete, damit die Historie bleibt |

Mandantenmodell: Alle Schlüssel gehören in Phase 1 dem einen PTI-Mandanten
und teilen dessen Begriffsliste und Vaults. Isolation zwischen Anwendungen ist
Phase 2 (§8) — dafür trägt der Schlüssel schon `tenant_id`, kein Schema-Umbau
nötig. **Es gibt in Phase 1 keine Isolation zwischen Schlüsseln:** ein
Schlüssel sieht über `GET /mappings` alle Zuordnungstabellen des Mandanten —
auch die, die ein Mensch in der Oberfläche oder eine andere Anwendung angelegt
hat — und kann sie lesen und weiterverwenden. Dass Vault-IDs zufällige UUIDs
sind, schützt daher nichts; die Liste gibt sie ohnehin heraus. Wer das nicht
will, arbeitet mit `"mode": "anonymous"`, dann entsteht keine
Zuordnungstabelle.

### 4.4 Aktor und Nutzungserfassung

`backend/core/deps.py` bekommt einen einheitlichen Aktor-Begriff:
`current_username(request)` liefert für den SSO-Pfad den Hub-Username, für
den API-Pfad `apikey:<label>`. `get_tenant(session, request)` liefert den
Mandanten des Schlüssels bzw. den Default. Damit landen API-Aufrufe in
`usage_events` mit erkennbarem Aktor — nie mit Inhalten.

Admin-Gates (`is_admin`): Der API-Schlüssel gilt innerhalb seines Mandanten
als vollberechtigt (Begriffe pflegen, Vaults anlegen und lesen). Begründung:
„alles, was die Anwendung anbietet" — und die aufrufende Anwendung ist eine
PTI-Anwendung, kein Fremder. Scopes sind bewusst nicht Teil dieser Etappe.

**Eine Ausnahme: `DELETE /mappings/{id}` bleibt allein `is_admin` vorbehalten**
und weist einen Schlüssel mit 403 ab. Das Löschen einer Zuordnungstabelle ist
die einzige unumkehrbare Operation der Anwendung; ein geleakter Schlüssel
dürfte nicht die Rückführbarkeit einer ganzen Organisation vernichten können.
Alles andere, was ein Schlüssel anrichtet, lässt sich nachträglich
korrigieren — deshalb genau hier die Grenze und nirgends sonst.

### 4.5 Verwaltung

Endpunkte (nur SSO, nur `role == admin`):

```
GET    /api/v1/api-keys            Liste (ohne Hash), neueste zuerst
POST   /api/v1/api-keys            {label, expires_at?} → 201 {…, "token": "<einmalig>"}
DELETE /api/v1/api-keys/{id}       Widerruf (setzt revoked_at)
```

Oberfläche: neuer Bereich **„API-Schlüssel"** (nur für Admins sichtbar,
gespeist aus `/api/me`). Tabelle mit Bezeichnung, Präfix, erstellt von/am,
zuletzt benutzt, Status; Formular „Neuen Schlüssel erzeugen" (Bezeichnung,
optionales Ablaufdatum); nach dem Erzeugen ein Feld mit dem Klartext-Token,
Kopieren-Knopf und dem Hinweis „wird nur jetzt angezeigt"; „Widerrufen" mit
Rückfrage. Dazu ein kurzer Doku-Kasten mit dem cURL-Beispiel.

### 4.6 Dokumentation

`API.md` im Repo-Root (Upstream hat keine solche Datei): Authentisierung,
alle Endpunkte mit Beispiel-Request/-Response (Text, Datei als Base64,
Rückführung, `entities`, Fehlerformat), Hinweis auf `/api/docs` (OpenAPI,
hinter SSO). Verweis aus `README.MD`.

## 5. Sicherheit

- Der Maschinenpfad ist vom SSO-Gate ausgenommen, **nicht** vom
  HTTPS-Redirect. Ein Klartext-HTTP-Aufruf mit Token wird umgeleitet, der
  Token ist dann allerdings schon einmal unverschlüsselt unterwegs gewesen —
  die Doku sagt ausdrücklich „nur https".
- Kein Rate-Limit in dieser Etappe (wie Translator). Der NGINX-Balancer ist
  der Ort dafür, falls nötig.
- Token nie loggen; im Fehlerfall nur `key_prefix`.
- `HUB_AUTH=off` betrifft nur den SSO-Pfad. Der `/api/ext/`-Pfad verlangt
  auch lokal einen gültigen Schlüssel — sonst würde ein Test grün, der in
  Produktion rot wäre.

## 6. Tests

- Backend: `entities`-Filter (nur gewählte Typen; KEEP wird bei expliziter
  Wahl ersetzt; unbekannter Typ → 400; leere Liste → 400), Katalog-Endpunkt,
  Schlüssel-Lebenszyklus (erzeugen → Liste zeigt keinen Klartext → Aufruf mit
  Token → Widerruf → 401 → abgelaufen → 401 → ohne Header → 401 → falscher
  Token → 401), Nicht-Admin darf keine Schlüssel anlegen (403), API-Pfad ohne
  Cookie funktioniert, `/api/v1` ohne Cookie weiterhin nicht (Gate-Test mit
  `HUB_AUTH` an), Nutzungseintrag trägt `apikey:<label>`, Rundlauf
  anonymize → reverse über `/api/ext/v1` (Text und Datei).
- Frontend: `tsc` und `vite build` müssen sauber durchlaufen; manuelle Prüfung
  im Browser (Kategorien-Checkboxen wirken; Dateipfad liefert Download).
- Golden-Set-Gate bleibt unverändert grün (Erkennung selbst wird nicht
  angefasst).

## 7. Nicht Teil dieser Etappe

Scopes pro Schlüssel, Rate-Limits, Mandanten-Isolation pro Anwendung,
Async-Jobs für grosse Dateien, Schlüsselrotation mit Überlappungsfrist.
