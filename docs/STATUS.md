# Stand des PTI-Pseudonymisierers

**Stand: 2026-08-28 (spätnachmittags).** Dieses Dokument ist die Ist-Aufnahme: Was läuft, was
ist gebaut aber ausgeschaltet, was ist bewusst zurückgestellt. Es ergänzt
`CLAUDE.md` (Arbeitsanweisungen) und die Design-Specs unter
`docs/superpowers/specs/` (das Warum hinter jeder Entscheidung).

Das Fachkonzept (§1–§15) liegt im Schwester-Repo `landing` unter
`frontend/public/ideen/pseudonymizer.html`.

---

## Umgesetzt und in Betrieb

| Phase | Was | Stand |
|---|---|---|
| **1** | Engine + Oberfläche, Erkennung/Ersetzung/Rückführung, DOCX/TXT/CSV, Vault | live |
| **2** | Kategorienwahl, externe API `/api/ext/v1` mit gehashten Schlüsseln, Nutzungsauswertung | live |
| **3** | KI-Schicht für Personennamen-Grenzen (Variante C, behebt den Namenssplit) | live |
| **5** | XLSX- und PPTX-Verarbeitung, formaterhaltend | live |
| **4** | Asynchrone Aufträge für grosse Dateien (Hintergrund-Arbeiter, Fortschritt, Auftragsgedächtnis) | live |
| **6** | **PDF mit Textebene** (Redaction statt Glyphen-Neusetzen), Metadaten leeren · **OCR für Scans** (Tesseract, lokal) | live |

Ergänzende Details je Bereich stehen in `CLAUDE.md`.

---

## Phase 6 (PDF/OCR) — genauer

**Zwei Staffeln, unterschiedlicher Reifegrad:**

1. **PDF mit Textebene — fertig und lauffähig.** Ein am Rechner erzeugtes
   PDF (Text steckt echt drin) wird erkannt, die Fundstelle per Redaction
   **aus dem Inhaltsstrom entfernt** (nicht nur überdeckt), das Pseudonym
   darüber gestempelt; Autor/Titel/XMP werden geleert. Läuft ohne
   Systembinary über PyMuPDF. `.pdf` bekommt kein `415` mehr.

2. **OCR für Scans — seit 2026-08-28 in Betrieb.** Ein eingescanntes PDF
   trägt nur Bilder; Texterkennung (Tesseract, lokal) macht daraus lesbaren
   Text. `OCR_ENABLED=true` steht im Dashboard, das Systembinary liegt auf
   dem Knoten (siehe unten), und `ocr_verfuegbar()` meldet in Produktion
   `True`.

**Die tragende Sicherheitsregel:** Ein Scan, dessen Text niemand lesen kann
(Bildseite, OCR aus), scheitert **laut** mit klarer Meldung — nie stumm.
Sonst sähe das Dokument ohne Funde pseudonymisiert aus, während der Klarname
als Bild darin steht. Das ist das gefährlichste denkbare Fehlerbild und ist
durch zwei Tests abgesichert.

Design und Begründungen: `docs/superpowers/specs/2026-08-28-pdf-ocr-design.md`.

---

## Offen — Betriebsschritte (nicht Code)

- ~~Tesseract auf dem Jelastic-Knoten installieren~~ — **erledigt am
  2026-08-28** (PR #29). Wie, und was dabei zu beachten ist, steht in
  `CLAUDE.md` unter „Tesseract auf dem Knoten".
- **API-Schlüssel in Produktion durchsehen**, falls die Ablösung von PPM und
  Translator auch dort gelten soll: Oberfläche → *API-Schlüssel* →
  gegebenenfalls widerrufen; unter *Nutzung* ist sichtbar, ob einer zuletzt
  aufgerufen hat. (Im Code besteht keine Kopplung mehr — nur die
  Produktionsdatenbank kennt die tatsächlich ausgestellten Schlüssel.)

---

## Offen — Messung und Entscheidung

- ~~Das Zeitbudget des Balancers ist ungemessen.~~ — **gemessen am
  2026-08-28: 300 Sekunden**, nicht 60. Ein Lauf über 280'000 Zeichen bekam
  nach exakt 300.5 s einen `504`; Läufe bis 127 s gingen durch. Gemessen
  über `/api/ext/v1` mit einem befristeten Schlüssel (widerrufen) und
  synthetischen Dokumenten, ohne die Produktion umzukonfigurieren.
  Zahlen und Auslegung in
  `docs/superpowers/specs/2026-08-25-asynchrone-auftraege-design.md` §12.

  **Daraus abgeleitet, entschieden am 2026-08-28:** `SYNC_THRESHOLD_CHARS`
  steht jetzt auf **100'000**, und `WORKERS` auf **3** statt 2.

  **Die Schwelle hängt an der Segmentzahl.** Über alle vier Formate
  gemessen: txt 395'000 Zeichen in ~123 s (~3'200 Z/s), aber docx 149'000
  in ~272 s, xlsx 100'000 in ~181 s, pptx 100'000 in ~184 s — die drei
  Office-Formate praktisch identisch bei ~545 Z/s. Ein `.txt` ist ein
  Segment, dieselbe Menge als Office-Datei sind 427 (je Segment ein
  spaCy-Lauf). Zwei frühere Fassungen (400'000, 150'000) waren zu hoch,
  beide aus dem jeweils günstigsten gemessenen Format abgeleitet. Lokal
  ist der Effekt nicht sichtbar; wer die Zahl neu ableitet, misst auf dem
  Knoten und mit einem Office-Format.

  **Die drei Worker haben gewirkt:** Mit zwei streuten dieselben 270'000
  Zeichen zwischen 63 s und 127 s, mit dreien zwischen 61 s und 64 s.
- **KI-Zweitmeinung (Stufe D) bleibt ab Werk aus.** Gemessen zu teuer für das
  Infomaniak-Kontingent; zuschalten lohnt erst, wenn die Anfragezahl gesenkt
  oder das Kontingent angehoben ist. Rechtlich getragen (AVV, 2026-08-25).

---

## Bewusst zurückgestellt (eigene Vorhaben)

- **Formaterhaltendes Glyphen-Neusetzen in PDF.** Wir schwärzen und stempeln
  das Pseudonym darüber — sicher, aber typografisch nicht immer perfekt. Das
  buchstabengenaue Einpassen ist laut Roadmap ein eigenes, grösseres Vorhaben.
- **OCR-Qualität auf echten Behörden-Scans.** Der Mechanismus ist getestet,
  die Erkennungsgüte auf realen (teils schlechten) Scans nicht — sinnvoll erst
  nach dem Tesseract-Einbau messbar, und nur mit synthetischen Dokumenten
  (keine echten Personendaten ins Repo).

---

## Erkennungsqualität (Golden-Set)

Marken gehalten (Messstand 2026-08-28, ohne KI-Schicht):
Gesamt-Precision 92.4 %, Gesamt-Recall 98.6 %, PERSON-Recall 96.7 %,
Struktur-PII (AHV/IBAN/E-Mail) 100 %. Ziel: Struktur-PII 100 %, Namen ≥ 90 %
Recall, Precision ≥ 90 %.

Bekannte Restfehlalarme vom Typ „Sachbegriff als Entität" (`Kontrollschild`,
`Fallnummer`, `Intern`, `Polizei-Abfrageplattform`) — nächster Ansatz wäre
Satzkontext. `franzoesische_namen` bleibt gemessene Reichweitengrenze
(deutsch trainiertes Modell), zählt nicht ins Gate.
