# Stand des PTI-Pseudonymisierers

**Stand: 2026-08-28 (nachmittags).** Dieses Dokument ist die Ist-Aufnahme: Was läuft, was
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

- **Das Zeitbudget des Balancers ist ungemessen.** Die Schwelle, ab der eine
  Datei als Hintergrund-Auftrag läuft, hängt an der Annahme „60 Sekunden"
  (NGINX-Vorgabe, nicht gemessen). Nachzumessen aus VS Code — Verfahren in
  `docs/superpowers/specs/2026-08-25-asynchrone-auftraege-design.md` §12.
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
