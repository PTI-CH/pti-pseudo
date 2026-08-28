"""Tests des PDF-Adapters — nur Textebene (Spec §7.1–§7.4).

Der OCR-Zweig (bildbasierte Seiten, Spec §4) ist bewusst **nicht** Teil
dieser Datei — er gehört zu einem parallelen Arbeitsstrang und hat seine
eigene Testdatei (``test_ocr_pdf.py``). Alle Testdokumente entstehen
programmatisch über ``pdf_fabrik.py``; es liegen keine Binärdateien im
Repository und, gemäss Hausregel, keine echten Personendaten. Die
verwendeten Namen sind frei erfunden, mit echten Umlauten (siehe
``golden-set/README.md`` zur Begründung).

Deckt die Abnahmepunkte aus
``docs/superpowers/specs/2026-08-28-pdf-ocr-design.md`` §7.1–§7.4 ab:

1. Roundtrip bei leerem ``replacements``.
2. Redaction entfernt den Klarnamen wirklich (nicht nur optisch).
3. Metadaten (Autor/Titel) sind nach ``strip_metadata`` leer.
4. Determinismus: zweimaliges ``extract_segments`` liefert Identisches.
5. Segmentierung: eine Zeile je Segment, überlappungsfrei, leere Zeilen
   entfallen.
6. Ungültige Bytes lösen ``ValueError`` aus.
7. Die ganze Pipeline (``anonymize_document``) ersetzt einen Namen, der in
   zwei Zeilen vorkommt, an beiden Stellen — die Fund-Übertragung
   (``propagate_findings``) greift auch für PDF.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters import get_adapter  # noqa: E402
from backend.adapters.base import Replacement  # noqa: E402
from backend.adapters.pdf_adapter import PdfAdapter, PdfLocation  # noqa: E402
from backend.tests.pdf_fabrik import baue_pdf, setze_metadaten  # noqa: E402

# ── Erwartete Inhalte (frei erfunden) ──────────────────────────────────────

ZEILE_1 = "Frau Corina Bühler wohnt in Zürich und arbeitet mit Fabian Näf zusammen."
ZEILE_2 = "Zweite Zeile ohne Personenbezug."
ZEILE_SEITE_2 = "Kontakt: Corina Bühler, Telefon 079 000 00 00."
AUTOR = "Testautor Muster"
TITEL = "Interner Entwurf"


def _get_text(data: bytes) -> str:
    """Liest den gesamten sichtbaren Text aller Seiten — unabhängig vom Adapter."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()


def _get_page_text(data: bytes, page_index: int) -> str:
    """Liest den Text einer einzelnen Seite — unabhängig vom Adapter."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return doc[page_index].get_text("text")
    finally:
        doc.close()


@pytest.fixture(scope="module")
def zweizeiliges_pdf() -> bytes:
    """Ein PDF mit zwei Seiten, mehreren Zeilen und einer echten Leerzeile."""
    return baue_pdf(
        [
            [ZEILE_1, "   ", ZEILE_2],
            [ZEILE_SEITE_2],
        ]
    )


@pytest.fixture(scope="module")
def adapter() -> PdfAdapter:
    """Der zu testende Adapter (zustandslos, daher modulweit teilbar)."""
    return PdfAdapter()


# ── 1. Roundtrip ohne Änderung (§7.1) ──────────────────────────────────────


def test_roundtrip_ohne_aenderung_erhaelt_den_inhalt(adapter, zweizeiliges_pdf):
    """Leeres ``replacements`` → über ``get_text`` identischer Inhalt.

    Byte-Gleichheit ist laut Vertrag (``adapters/base.py``) nicht zugesichert
    — ``doc.tobytes()`` packt neu.
    """
    ausgabe = adapter.apply_replacements(zweizeiliges_pdf, {})

    assert _get_text(ausgabe) == _get_text(zweizeiliges_pdf)

    # Und die Extraktion liefert dieselben Segmente wie vorher.
    vorher = adapter.extract_segments(zweizeiliges_pdf)
    nachher = adapter.extract_segments(ausgabe)
    assert [(s.id, s.text) for s in nachher] == [(s.id, s.text) for s in vorher]


def test_ergebnis_ist_ein_gueltiges_pdf(adapter, zweizeiliges_pdf):
    """Das zurückgeschriebene Dokument lässt sich wieder öffnen."""
    ausgabe = adapter.apply_replacements(zweizeiliges_pdf, {})
    doc = pymupdf.open(stream=ausgabe, filetype="pdf")
    try:
        assert doc.page_count == 2
    finally:
        doc.close()


# ── 2. Redaction entfernt wirklich (§7.2) ──────────────────────────────────


def test_redaction_entfernt_den_klarnamen_und_setzt_den_platzhalter(
    adapter, zweizeiliges_pdf
):
    """Nach ``apply_replacements`` steht der Klarname nicht mehr im PDF-Text.

    Geprüft wird der über ``get_text`` extrahierte Inhalt — also der
    Inhaltsstrom selbst, nicht nur, was sichtbar überdeckt ist. Genau das ist
    der Unterschied zwischen Redaction und einem optischen Balken.

    Die Fixture nennt „Corina Bühler" bewusst zweimal (Zeile 1 und die
    Kontaktzeile auf Seite 2, siehe ``test_redaction_wirkt_je_segment_
    unabhaengig`` weiter unten) — dieser Test ersetzt nur das erste
    Vorkommen und prüft deshalb gezielt **die betroffene Seite**, nicht das
    ganze Dokument.
    """
    segments = adapter.extract_segments(zweizeiliges_pdf)
    segment = next(s for s in segments if s.text == ZEILE_1)
    start = segment.text.index("Corina Bühler")
    end = start + len("Corina Bühler")

    ausgabe = adapter.apply_replacements(
        zweizeiliges_pdf,
        {segment.id: [Replacement(start=start, end=end, text="[[PERSON_1]]")]},
    )

    text = _get_page_text(ausgabe, segment.location.page_index)
    assert "Corina Bühler" not in text
    assert "[[PERSON_1]]" in text
    # Der Rest der Zeile bleibt erhalten.
    assert "Fabian Näf" in text
    assert "Zürich" in text


def test_redaction_wirkt_je_segment_unabhaengig(adapter, zweizeiliges_pdf):
    """Zwei Ersetzungen auf zwei verschiedenen Segmenten treffen beide Stellen."""
    segments = adapter.extract_segments(zweizeiliges_pdf)
    erste = next(s for s in segments if "Corina Bühler" in s.text and "wohnt" in s.text)
    zweite = next(s for s in segments if s.text == ZEILE_SEITE_2)

    def repl_fuer(segment, needle, ersatz):
        start = segment.text.index(needle)
        return Replacement(start=start, end=start + len(needle), text=ersatz)

    ausgabe = adapter.apply_replacements(
        zweizeiliges_pdf,
        {
            erste.id: [repl_fuer(erste, "Corina Bühler", "[[PERSON_1]]")],
            zweite.id: [repl_fuer(zweite, "Corina Bühler", "[[PERSON_1]]")],
        },
    )

    text = _get_text(ausgabe)
    assert "Corina Bühler" not in text
    assert text.count("[[PERSON_1]]") == 2


# ── 3. Metadaten weg (§7.3) ─────────────────────────────────────────────────


def test_strip_metadata_leert_autor_und_titel(adapter, zweizeiliges_pdf):
    """Autor/Titel sind nach ``strip_metadata`` leer."""
    mit_metadaten = setze_metadaten(zweizeiliges_pdf, autor=AUTOR, titel=TITEL)
    doc_vorher = pymupdf.open(stream=mit_metadaten, filetype="pdf")
    try:
        assert doc_vorher.metadata["author"] == AUTOR
        assert doc_vorher.metadata["title"] == TITEL
    finally:
        doc_vorher.close()

    bereinigt = adapter.strip_metadata(mit_metadaten)
    doc_nachher = pymupdf.open(stream=bereinigt, filetype="pdf")
    try:
        assert doc_nachher.metadata["author"] == ""
        assert doc_nachher.metadata["title"] == ""
    finally:
        doc_nachher.close()

    # Der Fliesstext bleibt unberührt — nur Metadaten wurden angefasst.
    assert _get_text(bereinigt) == _get_text(zweizeiliges_pdf)


# ── 4. Determinismus ────────────────────────────────────────────────────────


def test_extraktion_ist_deterministisch(adapter, zweizeiliges_pdf):
    """Zweimaliges ``extract_segments`` auf identischen Bytes → Identisches.

    Sonst zeigen Offsets aus einem früheren Request beim Anwenden ins Leere
    (Determinismus-Pflicht, ``adapters/base.py``).
    """
    erst = adapter.extract_segments(zweizeiliges_pdf)
    zweit = adapter.extract_segments(zweizeiliges_pdf)

    assert [(s.id, s.text, s.location) for s in erst] == [
        (s.id, s.text, s.location) for s in zweit
    ]


# ── 5. Segmentierung ─────────────────────────────────────────────────────────


def test_mehrzeiliges_pdf_ergibt_mehrere_ueberlappungsfreie_segmente(
    adapter, zweizeiliges_pdf
):
    """``page{p}/line{l}``-IDs, eindeutig und ohne Überlappung."""
    segments = adapter.extract_segments(zweizeiliges_pdf)

    ids = [s.id for s in segments]
    assert len(ids) == len(set(ids)), f"Doppelte Segment-IDs: {ids}"
    assert all(i.startswith("page") and "/line" in i for i in ids)

    # Zwei Seiten mit Text -> Segmente auf beiden Seitenindizes.
    seiten = {s.location.page_index for s in segments}
    assert seiten == {0, 1}

    # Locations sind eindeutig (kein Absatz/keine Zeile doppelt erfasst).
    locations = [(s.location.page_index, s.location.line_index) for s in segments]
    assert len(locations) == len(set(locations))

    texte = [s.text for s in segments]
    assert ZEILE_1 in texte
    assert ZEILE_2 in texte
    assert ZEILE_SEITE_2 in texte


def test_leere_zeilen_erzeugen_keine_segmente(adapter, zweizeiliges_pdf):
    """Die Leerraum-Zeile zwischen ``ZEILE_1`` und ``ZEILE_2`` bleibt aussen vor."""
    segments = adapter.extract_segments(zweizeiliges_pdf)

    for segment in segments:
        assert segment.text.strip(), f"Segment {segment.id!r} ist leer/whitespace"

    # Vorbedingung des Tests: Die Fabrik hat tatsächlich eine Leerraum-Zeile
    # zwischen den beiden Textzeilen der ersten Seite platziert (siehe
    # pdf_fabrik.py, Modulkopf) — ohne sie prüft dieser Test nichts.
    seite_0 = sorted(
        (s.location.line_index for s in segments if s.location.page_index == 0)
    )
    assert seite_0 == [0, 2], (
        "erwartet Zeile 0 (Text) und Zeile 2 (Text), Zeile 1 "
        "(Leerraum) muss als Segment fehlen"
    )


# ── 6. Ungültige Bytes ──────────────────────────────────────────────────────


def test_ungueltige_bytes_werfen_value_error(adapter):
    """Kaputte/fremde Bytes → ``ValueError``, wie im Adapter-Vertrag gefordert."""
    with pytest.raises(ValueError):
        adapter.extract_segments(b"Das hier ist offensichtlich kein PDF.")


def test_ungueltige_bytes_werfen_value_error_auch_beim_anwenden(adapter):
    """Dieselbe Zusicherung für ``apply_replacements``."""
    with pytest.raises(ValueError):
        adapter.apply_replacements(b"kein pdf", {})


def test_ungueltige_bytes_werfen_value_error_auch_bei_strip_metadata(adapter):
    """Dieselbe Zusicherung für ``strip_metadata``."""
    with pytest.raises(ValueError):
        adapter.strip_metadata(b"kein pdf")


# ── Registry ─────────────────────────────────────────────────────────────


def test_registry_liefert_den_pdf_adapter_fuer_endung_und_mime():
    """Die Registry (``adapters/__init__.py``) kennt PDF seit dieser Etappe."""
    assert type(get_adapter("Bericht.pdf")).__name__ == "PdfAdapter"
    assert type(get_adapter("application/pdf")).__name__ == "PdfAdapter"


def test_location_ist_ein_frozen_dataclass_mit_primitiven_werten(
    adapter, zweizeiliges_pdf
):
    """``PdfLocation`` trägt keine Glyphen-Boxen (Determinismus-Pflicht)."""
    segment = adapter.extract_segments(zweizeiliges_pdf)[0]
    assert isinstance(segment.location, PdfLocation)
    assert isinstance(segment.location.page_index, int)
    assert isinstance(segment.location.line_index, int)
    assert segment.location.quelle == "text"


# ── 7. Die ganze Pipeline (§7.4) ─────────────────────────────────────────────


def test_pipeline_ersetzt_beide_vorkommen_ueber_zeilengrenzen(monkeypatch):
    """Ein Name über zwei Zeilen — die Fund-Übertragung greift auch für PDF.

    ``NAME`` steht in einem ausformulierten Satz (dort erkennt das echte
    spaCy-Modell ihn) und in einer kontextlosen Stichwortzeile auf der
    zweiten Seite (dort nicht) — dasselbe Fehlerbild wie
    ``golden-set/synthetic/generator.py::_baue_wiederholte_nennung`` und
    ``test_pipeline_propagation.py``. Ohne ``pipeline.propagate_findings``
    bliebe die zweite Stelle unpseudonymisiert im PDF stehen; das ist genau,
    was dieser Test bewacht.

    Lädt das echte spaCy-Modell — akzeptiert, wie in
    ``test_pipeline_propagation.py`` und ``test_golden_set.py``.
    ``vault=None`` — anonymer Modus, der Vault wird nie benutzt, trotzdem
    steht ``VAULT_MASTER_KEY``, wie in der Aufgabenstellung verlangt.
    """
    monkeypatch.setenv("VAULT_MASTER_KEY", "0" * 64)

    from backend import pipeline

    name = "Talblick"
    satz = (
        f"Die Firma {name} gibt zwar im Zusammenspiel mit dem Verein "
        "einen Releasetakt vor."
    )
    stichwortzeile = f"Vertragspartner: {name}"
    daten = baue_pdf([[satz], [stichwortzeile]])

    ausgabe, _detection, funde = pipeline.anonymize_document(
        session=None,
        vault=None,
        data=daten,
        filename="vertrag.pdf",
    )

    assert len(funde) == 2, "beide Vorkommen muessen als Fund gezaehlt sein"
    assert any(f.propagated for f in funde), (
        "die Stichwortzeile darf nur ueber die Uebertragung gefunden worden "
        "sein, sonst hat das Modell sie selbst erkannt und der Test prueft "
        "nicht mehr, was er soll"
    )
    assert any(not f.propagated for f in funde), (
        "mindestens ein Fund muss der echte, nicht uebertragene sein"
    )

    text = _get_text(ausgabe)
    assert name not in text
    # Der schmale Redaction-Rechteck einer Wort-Zeile lässt ``insert_textbox``
    # mitunter mitten im Platzhalter umbrechen (Spec §3.2, „Schrift-
    # verkleinerung, dann Abschneiden") — für den Nachweis „Platzhalter
    # zweimal vorhanden" werden Zeilenumbrüche deshalb vor dem Zählen entfernt.
    ohne_umbrueche = text.replace("\n", "")
    assert ohne_umbrueche.count("[[ORGANIZATION]]") == 2
