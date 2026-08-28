# Externe API des PTI-Pseudonymisierers

Diese Datei richtet sich an alle, die Texte und Dokumente über den
Maschinenpfad des Pseudonymisierers verarbeiten wollen — dieselbe
Funktionalität wie die Oberfläche, ohne Anmeldung am TIP-Hub.

**Der Pseudonymisierer ist ein eigenständiges Werkzeug.** Diese Schnittstelle
ist ein Angebot nach aussen, keine Verabredung mit einer bestimmten
Anwendung: Es gibt keinen gesetzten Abnehmer, keine Anwendung, mit der das
Werkzeug zusammenarbeiten *muss*, und nichts hier setzt voraus, dass die
aufrufende Seite überhaupt zum PTI-Umfeld gehört. Wer einen gültigen
Schlüssel hat, ruft auf; wer keinen hat, nicht.

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
| `404` | Zuordnungstabelle, Begriff oder Auftrag nicht gefunden (auch wenn er existiert, aber einem anderen Aktor/Mandanten gehört) |
| `409` | Der Begriff steht bereits in der Liste, oder sein festes Pseudonym ist schon einem anderen Begriff zugeordnet (`POST /terms`) — oder ein Auftrag ist noch nicht fertig (`GET /jobs/{id}/result`) |
| `413` | Datei oder Text über dem Grössenlimit (siehe unten) |
| `415` | Dateiformat nicht unterstützt oder kann nicht verarbeitet werden — **unabhängig von der Dateigrösse**, auch oberhalb der Auftragsschwelle. PDF-Dateien mit Textebene werden unterstützt; gescannte PDFs schlagen hier fehl, wenn OCR auf der Installation nicht aktiviert ist (siehe „PDF-Unterstützung") |
| `422` | Pydantic-Validierung (z. B. Pflichtfeld fehlt) |
| `503` | Datei oberhalb der Auftragsschwelle, aber diese Installation fährt keinen Hintergrundarbeiter (`anonymize-file`, `reverse-file`) |

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
      "text": "Andreas Meier",
      "propagated": false
    }
  ],
  "char_count": 40,
  "ki_geprueft": false
}
```

`confidence` ist eine Ampelstufe (`gruen` ≥ 0.85, `gelb` ≥ 0.5, sonst `rot`).

`propagated` ist `true`, wenn dieser Fund nicht von der Erkennung selbst
stammt, sondern von einer gleichlautenden Stelle übernommen wurde. Hintergrund:
Das NER-Modell entscheidet je Vorkommen und meldet denselben Namen im einen
Absatz, im nächsten nicht. Ersetzt würde sonst nur die gemeldete Stelle — der
Klarname bliebe im Dokument stehen, obwohl er im Befund steht. Für die
Ersetzung sind beide Arten gleichwertig; das Feld ist reine Auskunft über die
Herkunft. Übertragen werden nur `PERSON`, `LOCATION` und `ORGANIZATION`:
Muster- und Begriffslisten-Erkenner finden ihren Text ohnehin an jeder Stelle.

#### KI-Prüfung der Namensgrenzen

Alle drei Verarbeitungs-Endpunkte nehmen ein optionales **`ki_pruefung`**
entgegen (JSON: `"ki_pruefung": true`; Multipart: Formularfeld
`ki_pruefung=true`).

Ist es gesetzt, prüft ein Schweizer KI-Dienst (Infomaniak AI) die Grenzen der
gefundenen Personennamen. Das behebt den häufigsten Fehler der lokalen
Erkennung: einen halb erkannten Namen (`Sandra` statt `Sandra Zbinden`), bei
dem der Nachname im Dokument stehen bleibt und das Ergebnis trotzdem
pseudonymisiert *aussieht*.

**Dabei verlassen kurze Textausschnitte den Server** — rund 20 Wörter um jede
gefundene Stelle, nie das ganze Segment und nie das Dokument.

**Die Vorgabe ist `false`.** Das ist Absicht: Dieser Maschinenpfad hängt
dieselben Router ein wie die Oberfläche. Wäre die Vorgabe `true`, begänne jede
anbindende Anwendung ab einem Deploy stillschweigend, Dokumentinhalte
hinauszuschicken. Wer den Datenversand will, fordert ihn an.

Jede Antwort trägt **`ki_geprueft`**. `false` heisst: nicht angefordert, nicht
konfiguriert — **oder der Dienst hat nicht geantwortet**. Im letzten Fall
enthält die Antwort trotzdem die vollständige lokale Fundliste; der Lauf
scheitert nicht. Wer auf die KI-Prüfung angewiesen ist, muss das Feld auswerten.

„Nichts zu prüfen" gehört seit dem 25.08.2026 **nicht** mehr dazu: Ein Text
ohne Fundkandidaten liefert `true`, weil die Schicht vollständig gelaufen ist.
Ein Hinweis, der ohne Grund erscheint, bringt Menschen dazu, Hinweise zu
übergehen — und dieses Feld ist genau dafür da, gelesen zu werden.

**Der Ausfall ist still.** Am 25.08.2026 antwortete der Dienst plötzlich in
Prosa statt in JSON, ohne dass sich an unserem Code etwas geändert hätte: Die
KI-Prüfung lief einen halben Tag lang wirkungslos, während die Antworten
weiterhin gültig aussahen. Genau dagegen ist dieses Feld die einzige
Sicherung — wer es nicht auswertet, merkt einen solchen Fall nie.

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

Pseudonymisiert ein Dokument formaterhaltend (DOCX, XLSX, PPTX, CSV, TSV, TXT, PDF). Multipart statt
JSON, weil eine Datei mitgeschickt wird. Die Antwort trägt das Ergebnis als
Base64, nicht als Datei-Download — daneben steht in derselben Antwort auch der
Befund.

**Bei einer grossen oder textreichen Datei liefert dieser Endpunkt `202` statt
des Ergebnisses** — massgeblich sind `SYNC_THRESHOLD_BYTES` (2 MiB) **und**
`SYNC_THRESHOLD_CHARS` (300'000 Zeichen); siehe „Grosse Dateien: asynchrone
Aufträge" weiter unten.

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:anonymize-file \
  -H "X-Api-Key: ptps_…" \
  -F "file=@brief.docx" \
  -F "mode=reversible" \
  -F "vault_label=Anfrage #4711" \
  -F "entities=PERSON,CH_IBAN"
```

`selected_entities` gibt es auch hier — als JSON-Zeichenkette, weil ein
Multipart-Formular keine Listen von Objekten kennt. Je Eintrag mindestens
`segment_id`, `start` und `end`, so wie sie ein vorheriger Lauf **über
dieselbe Datei** geliefert hat:

```bash
  -F 'selected_entities=[{"segment_id":"body/p0","start":5,"end":18}]'
```

Achtung, das ist die häufigste Fehlerquelle bei diesem Feld: Die Segment-IDs
stammen vom Adapter des jeweiligen Formats. Eine Auswahl aus einem
`:detect`-Lauf (dort heisst das Segment immer `text`) passt **nicht** auf einen
Dateilauf. Passt kein Eintrag, wird nichts ersetzt — der Aufruf meldet
trotzdem `200`, erkennbar allein an `applied: 0`.

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

**Dieselbe Schwelle wie bei `:anonymize-file`** — ein grosses Dokument
zurückzuführen heisst, es erneut zu zerlegen und zusammenzusetzen, also
dasselbe Timeout-Risiko wie beim Pseudonymisieren.

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

### Grosse Dateien: asynchrone Aufträge

`documents:anonymize-file` und `documents:reverse-file` verarbeiten eine grosse
Datei nicht mehr im laufenden Request, sondern als Auftrag im Hintergrund.
Ohne diesen Umweg liefe die Verarbeitung in den Gateway-Timeout, lange bevor
sie fertig ist.

**Zwei Schranken entscheiden das, seit dem 28.08.2026** (beides
Server-Einstellungen, nicht über die API abfragbar, nur an ihrer Wirkung
erkennbar):

| Einstellung | Default | Geprüft |
|---|---|---|
| `SYNC_THRESHOLD_BYTES` | 2 MiB | an der Dateigrösse, ohne die Datei zu öffnen |
| `SYNC_THRESHOLD_CHARS` | 300'000 | an der Textmenge, nach dem Adapterlauf |

Überschreitet eine Datei **eine** davon, wird sie zum Auftrag.

**Warum zwei.** Die Verarbeitungsdauer hängt am Text, nicht an der
Dateigrösse — gemessen rund 20'000 Zeichen je Sekunde über alle Formate.
Zwischen beidem liegt der Formatanteil, und der ist gewaltig: Dieselben
64'107 Zeichen wiegen als `.txt` 64 KiB, als `.docx` 39 KiB und als `.xlsx`
8 KiB. Eine Byte-Schranke allein liess dadurch genau die teuren Dateien
durch: Eine dichte Arbeitsmappe von 2 MiB trug über 17 Millionen Zeichen und
war rund 15 Minuten unterwegs.

**Was das für einen Aufrufer bedeutet.** Eine kleine, aber textreiche Datei
kann jetzt `202` bekommen, wo sie vorher `200` bekam — die Dateigrösse allein
sagt nicht mehr voraus, welche der beiden Antworten kommt. Wer `:anonymize-file`
oder `:reverse-file` aufruft, muss **beide** Antwortformen behandeln, und zwar
unabhängig davon, wie gross die Datei ist. Das war schon vorher so; seit dem
28.08.2026 trifft es nur mehr Dateien.

**Betroffen sind ausschliesslich diese beiden Dateirouten.** Die drei
Textrouten (`:detect`, `:anonymize`, `:reverse`) bleiben in jedem Fall
synchron — sie sind durch `max_text_chars` (300'000 Zeichen) begrenzt und
laufen in Sekundenbruchteilen; ein Auftrag wäre dort ein Rückschritt für die
häufigste Nutzung.

Oberhalb der Schwelle antwortet die Route mit **`202 Accepted`** statt mit dem
gewohnten Ergebnis:

```json
{"job_id": "3f2a1c9e-7425-40de-944b-e07fc1f90ae7", "status": "wartend"}
```

**Nicht jede Installation kann das.** Der Hintergrundarbeiter braucht
PostgreSQL (siehe CLAUDE.md, Abschnitt „Asynchrone Aufträge"); auf einer
Installation ohne Hintergrundarbeiter liefert eine Datei oberhalb der
Schwelle **`503 Service Unavailable`** statt `202` — **keine** `job_id`,
**keine** Auftragszeile:

```json
{"detail": "Grosse Dateien werden im Hintergrund verarbeitet; das ist auf dieser Installation nicht eingerichtet. Bitte teilen Sie die Datei auf, oder wenden Sie sich an die Administration."}
```

Bewusst 503, nicht 500: Es liegt nicht an der Anfrage, sondern daran, dass
diese Installation die Hintergrundverarbeitung nicht anbietet. Eine frühere
Fassung legte hier trotzdem einen Auftrag an — der blieb dann für immer auf
`wartend` stehen, ohne dass irgendwo stand, warum. Diese Abweisung entsteht
in `warteschlange.lege_an` selbst, nicht erst in der Route, damit sie kein
künftiger zweiter Aufrufer versehentlich umgehen kann.

**Ein nicht unterstütztes Format ergibt `415`, auch hier — und vor dem 503.**
Das Format wird geprüft, bevor überhaupt eine Auftragszeile entsteht. Ein
gescanntes PDF (ohne Text-Layer) schlägt hier fehl, wenn OCR auf der
Installation nicht aktiviert ist — der Server erkennt es als bildbasiert und
würde es mit deutlicher Fehlermeldung ablehnen:

```json
{"detail": "Dieses PDF enthält gescannte Seiten; die Texterkennung ist auf dieser Installation nicht aktiviert. Bitte als DOCX/TXT hochladen oder die Administration kontaktieren."}
```

Ein PDF mit Textebene verarbeitet sich wie ein DOCX — der Adapter entfernt
Text und Metadaten mittels Redaction. Ein weiterhin nicht unterstütztes Format
(z. B. `.doc`, `.xls`, `.rtf`) bekommt:

```json
{"detail": "Nicht unterstütztes Dateiformat. Unterstützt werden: .docx, .xlsx, .pptx, .csv, .tsv, .txt, .text, .md, .log, .pdf."}
```

(Der Zusatz „Unterstützt werden: …" hängt nur `:anonymize-file` an, wie im
synchronen Pfad; `:reverse-file` liefert den Formattext allein.) Ein gescanntes
PDF oberhalb der Auftragsschwelle bekommt denselben Text wie unterhalb der
Schwelle und keine `job_id`. Auf einer Installation ohne Hintergrundarbeiter
geht die Formatprüfung dem `503` vor — „bitte teilen Sie die Datei auf" ist
für eine gescannte PDF kein brauchbarer nächster Schritt, aber „bitte laden Sie
ein Textbild-PDF oder exportieren Sie als DOCX" schon.

#### PDF-Unterstützung

Der Pseudonymisierer unterstützt PDF-Dateien in zwei Staffeln:

1. **Text-Layer (immer)**: Ein am Rechner erzeugtes PDF trägt Textebene —
   der Text steht als solcher im Dokument, mit Positionsangaben. Der Adapter
   liest den Text wie aus einem DOCX, erkennt Personendaten und entfernt die
   Textbereiche mittels Redaction (nicht nur optisch überdeckt, sondern aus dem
   Inhaltsstrom gelöscht). Metadaten (Autor, Titel, XMP) werden geleert.

2. **OCR für Scans (optional)**: Ein eingescanntes PDF trägt kein Text-Layer,
   nur Bilder je Seite. Stellt der Adapter fest, dass eine Seite keinen
   nennenswerten Text enthält, rastert er sie zu einem Bild. Ist OCR verfügbar
   (Schalter `OCR_ENABLED=true` **und** das Systembinary `tesseract` mit
   Sprachdaten `deu` auf dem Knoten), erkennt Tesseract den Text im Bild und
   der Adapter verarbeitet es wie Text-Layer. Ist OCR nicht verfügbar, bricht
   der Lauf mit deutlicher Fehlermeldung ab (siehe „Fehlerformat" oben) — ein
   halb erkanntes Dokument, das trotzdem pseudonymisiert aussieht, ist
   schlimmer als gar keines.

OCR läuft vollständig lokal — kein Byte des Dokumentinhalts verlässt den
Server. Das ist ein bewusstes Design-Merkmal: Ein Cloud-OCR-Dienst würde die
gesamte gescannte PDF nach aussen schicken und dem Datenschutz-Anspruch des
Werkzeugs widersprechen.

#### `GET /jobs/{id}`

Status des Auftrags, **ohne Dokumentinhalte** — weder Fundtexte noch
Auszüge:

```json
{
  "id": "3f2a1c9e-7425-40de-944b-e07fc1f90ae7",
  "status": "laeuft",
  "mode": "anonymize",
  "dateiname": "protokoll.docx",
  "fortschritt": 42,
  "gesamt": 130,
  "fehler": null,
  "erstellt": "2026-08-25T09:00:00+00:00",
  "laeuft_ab": "2026-08-26T09:00:00+00:00"
}
```

`status` durchläuft `wartend` → `laeuft` → `fertig` oder `fehler`.

`fortschritt` und `gesamt` zählen **Segmente**, seit dem 28.08.2026. Ein
Segment ist die Einheit, die der Format-Adapter liefert — ein Absatz im
DOCX, eine Zeile im XLSX, ein Textrahmen im PPTX — und die Einheit, an der
die Arbeit hängt: je Segment ein Lauf des Sprachmodells.

Drei Dinge, auf die sich ein Aufrufer verlassen darf, und zwei, auf die
nicht:

* Der Zähler ist **monoton** und erreicht am Ende `gesamt`. Auch ein leeres
  Segment wird mitgezählt — sonst bliebe er in einem Dokument mit vielen
  Leerabsätzen ohne Grund stehen.
* Er wird **höchstens alle zwei Sekunden** neu geschrieben; der letzte Stand
  geht immer durch. Öfter zu fragen bringt nichts.
* Während `wartend` sind beide `null` — es ist noch nichts gezählt.
* **`fortschritt == gesamt` heisst nicht „gleich fertig".** Gezählt wird nur
  der Erkennungsdurchlauf. Was danach kommt (KI-Schicht, Übertragung
  gleichlautender Stellen, formaterhaltendes Zurückschreiben) läuft nicht je
  Segment und wird nicht gezählt. Der Zähler steht also eine Weile auf
  `gesamt`, bevor `status` auf `fertig` springt. Wer den Abschluss braucht,
  fragt `status` — nie den Zähler.
* **Segmente sind zwischen Formaten und Dokumenten nicht vergleichbar.** Ein
  DOCX-Absatz ist keine XLSX-Zeile; 500 Segmente sagen für sich genommen
  nichts über die Dauer. Vergleichbar ist ein Lauf nur mit sich selbst.

Eine Seitenzahl gibt es weiterhin nicht, und sie wird auch für DOCX und XLSX
nie kommen: Seiten entstehen dort erst beim Setzen und stehen gar nicht in
der Datei. Bis zum 28.08.2026 warteten die beiden Felder auf genau diese
Zahl und standen deshalb für jedes Format dauerhaft auf `null`.

`laeuft_ab` ist **ab der Anlage** gesetzt, nicht erst am Ende: 24 Stunden ab
jetzt, beim Übergang auf `fertig` oder `fehler` neu auf 24 Stunden ab dann
geschoben. Danach ist der Auftrag samt Ergebnis verschwunden. (Bis zum
26.08.2026 stand das Feld während `wartend`/`laeuft` auf `null` — ein
Aufrufer, der sich darauf verlassen hat, um „schon fertig?" zu erkennen,
muss `status` fragen.)

`fehler` ist nur bei `status: "fehler"` gefüllt und trägt **einen kuratierten
deutschen Satz für den Menschen**, keine Ausnahme-Wortlaute aus dem
Innenleben: entweder die Meldung des Formatregisters (etwa der PDF-Text
oben), eine der wenigen bewusst formulierten Meldungen des Auftragspfads
(wörtlich: „Für die Rückführung wird eine vault_id benötigt.",
„Zuordnungstabelle nicht gefunden.", „Die Verarbeitung wurde mehrfach
unterbrochen und aufgegeben. Bitte laden Sie die Datei erneut hoch.") — oder
sonst ein allgemeiner Satz:

```json
{"status": "fehler", "fehler": "Die Datei konnte nicht verarbeitet werden. Bitte prüfen Sie, ob sie sich in Ihrem Programm öffnen lässt, und wenden Sie sich sonst an die Administration.", "...": "..."}
```

Bis zum 26.08.2026 stand hier `str()` jeder beliebigen Ausnahme, also auch
der Wortlaut von lxml, openpyxl oder python-docx. Das war ein Kanal, über den
irgendwann Dateipfade oder Textausschnitte nach draussen gelangt wären — und
der ganze Auftragspfad existiert, um genau das nicht zu tun. **Werten Sie
`fehler` nicht maschinell aus**; die Texte sind für Menschen gedacht und
ändern sich. Der vollständige Wortlaut steht im Server-Protokoll.

#### `GET /jobs/{id}/result`

Liefert das fertige Ergebnis — **nur bei `status: "fertig"`**, sonst `409
Conflict` mit dem aktuellen Status im Rumpf:

```json
{"detail": {"status": "laeuft", "fehler": null, "meldung": "Der Auftrag ist noch nicht fertig."}}
```

Bei Erfolg trägt die Antwort **die Form des synchronen Pfads und zwei Felder
mehr** — nicht nur das Dokument, sondern auch Fundliste, angewendete Zahl,
Tresor-Kennung und die Einstellungen, mit denen der Lauf gestartet wurde:

```json
{
  "filename": "protokoll.docx",
  "content_base64": "UEsDBBQAAAAIAA…",
  "entities": [ { "...": "..." } ],
  "applied": 3,
  "vault_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "mode": "reversible",
  "char_count": 8213,
  "ki_geprueft": false,
  "requested_entities": ["PERSON", "CH_AHV", "LOCATION"],
  "ki_pruefung": true
}
```

(Bei einem Auftrag aus `:reverse-file` entsprechend schmaler: nur `filename`,
`content_base64`, `vault_id` — wie bei `POST /documents:reverse-file` selbst.)

Ohne die Fundliste könnte bei einer grossen Datei niemand einen Fehlalarm
abwählen; ohne `vault_id` wäre ein umkehrbar pseudonymisiertes Dokument nie
wieder zurückführbar, weil die Zuordnung nur verschlüsselt in der Datenbank
liegt.

**`requested_entities` und `ki_pruefung` hat der synchrone Pfad nicht** — die
Antwort eines Auftrags ist insofern eine *Obermenge* seiner Antwort, keine
Kopie. Der Grund: Wer synchron aufruft, kennt seine eigenen Parameter noch;
wer einen Auftrag abholt, hat unter Umständen nur die Kennung — die Oberfläche
etwa nach einem Seitenneuladen. Beide Felder geben die **Anfrage** wieder, nicht
das Ergebnis:

| Feld | Bedeutung |
|---|---|
| `entities` | die **gefundenen** Stellen (Fundliste) |
| `requested_entities` | die **gesuchten** Kategorien; `null` = keine ausdrückliche Wahl, es galt die Vorgabe des Servers |
| `ki_pruefung` | die KI-Prüfung war **angefordert** |
| `ki_geprueft` | die KI-Prüfung ist **tatsächlich gelaufen** (`false` auch bei Ausfall des Dienstes) |

Der nächste Aufruf mit der geprüften Auswahl (`selected_entities`) muss
dieselben Werte mitschicken wie der erste — sonst sucht der Server anders, als
die Fundliste behauptet, und ein `anonymous`-Lauf bekäme beim zweiten Anlauf
einen Tresor, den niemand bestellt hat.

Ältere Aufträge (bis 24 Stunden nach einem Deploy) tragen die beiden Felder
noch nicht; sie fehlen dann. Werten Sie ein fehlendes Feld nicht als Vorgabewert
aus — „unbekannt" und „`reversible`" sind zweierlei.

#### Sichtbarkeit

**Ein Aufrufer sieht ausschliesslich seine eigenen Aufträge**, geprüft gegen
den Aktor — den Benutzernamen in der Oberfläche, `apikey:<Schlüssel-ID>`
über diese API — nicht nur gegen den Mandanten. Ein fremder oder unbekannter
Auftrag liefert **`404`, nicht `403`**: Ein `403` würde verraten, dass der
Auftrag überhaupt existiert.

Beide Routen hängen wie alle anderen unter `/api/v1` **und** `/api/ext/v1`.

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
{"detail": "Zuordnungstabellen löschen ist nur Administratoren in der Oberfläche vorbehalten — nicht über API-Schlüssel."}
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
    {"id": "…", "term": "Projekt Nordwind", "entity_type": "ORGANIZATION", "label": "Organisation", "match_mode": "normalized", "replacement": null, "created_at": "2026-08-18T09:00:00+00:00"}
  ],
  "entity_types": [{"value": "PERSON", "label": "Person"}, {"value": "...", "label": "..."}]
}
```

```bash
curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/terms \
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \
  -d '{"term": "Projekt Nordwind", "entity_type": "ORGANIZATION", "match_mode": "normalized", "replacement": "[[PROJEKT_N]]"}'
```

`match_mode`: `exact` (achtet auf Gross-/Kleinschreibung) oder `normalized`
(gleicht Umlautschreibweisen und Leerraum an, Default).

`replacement` ist das **feste Pseudonym**: statt eines gezählten
`[[ORGANIZATION_3]]` bekommt dieser Begriff immer denselben Ersatztext.
Weglassen oder `null` heisst: zählen wie sonst. Höchstens 100 Zeichen — der Wert
landet unverändert in der Zuordnungstabelle, deren Spalte genau so breit ist.

Drei Eigenschaften, die man kennen muss:

- **Je Mandant eindeutig.** Ein bereits vergebenes festes Pseudonym wird mit
  `409` abgelehnt: zwei Klartexte hinter demselben Symbol wären nach
  `documents:reverse` nicht mehr auseinanderzuhalten.
- **Bestehende Zuordnungen bleiben.** Wer einem Begriff nachträglich ein festes
  Pseudonym gibt, ändert nichts an Vaults, die den Begriff schon kennen — sonst
  zeigte ein bereits ausgeliefertes Dokument auf ein Symbol, das der Vault nicht
  mehr führt.
- **Die Form ist Ihre Verantwortung.** Ein Ersatztext, der auch als gewöhnliche
  Wortfolge vorkommen kann (`Firma A`), wird beim Zurückführen **überall** im
  Dokument durch den Klarnamen ersetzt — auch dort, wo er harmlos gemeint war.
  Die doppelte Klammerform kommt in natürlichem Text praktisch nicht vor.

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
