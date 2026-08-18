"""Tests des Text- und CSV-Adapters sowie der Adapter-Registry.

Schwerpunkt ist die Zeichensatz-Erkennung: UTF-8, UTF-8 mit BOM und cp1252
müssen den Roundtrip überstehen, ohne dass sich das Encoding ändert. Eine
cp1252-Datei, die als UTF-8 zurückkommt, sieht für den Sachbearbeiter aus wie
ein kaputtes Werkzeug.

Alle Namen sind frei erfunden (Hausregel: keine echten Personendaten im Repo).
"""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters import (  # noqa: E402
    CsvAdapter,
    DocxAdapter,
    TextAdapter,
    UnsupportedFormatError,
    get_adapter,
    supported_formats,
)
from backend.adapters.base import Replacement  # noqa: E402
from backend.adapters.text_adapter import detect_encoding  # noqa: E402

BOM = codecs.BOM_UTF8

# Enthält bewusst Zeichen, die in cp1252 und UTF-8 unterschiedlich kodiert sind.
TEXT = (
    "Andreas Meier grüsst aus Zürich.\n"
    "Beat Vogel ist zuständig für Solothurn.\n"
)


# ── Zeichensatz-Erkennung ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "roh, erwartet_encoding, erwartet_bom",
    [
        (TEXT.encode("utf-8"), "utf-8", False),
        (BOM + TEXT.encode("utf-8"), "utf-8", True),
        (TEXT.encode("cp1252"), "cp1252", False),
    ],
    ids=["utf-8", "utf-8-bom", "cp1252"],
)
def test_encoding_erkennung(roh, erwartet_encoding, erwartet_bom):
    """Die drei Stufen der Erkennung greifen in der richtigen Reihenfolge."""
    assert detect_encoding(roh) == (erwartet_encoding, erwartet_bom)


@pytest.mark.parametrize(
    "roh",
    [
        TEXT.encode("utf-8"),
        BOM + TEXT.encode("utf-8"),
        TEXT.encode("cp1252"),
    ],
    ids=["utf-8", "utf-8-bom", "cp1252"],
)
def test_text_roundtrip_erhaelt_encoding(roh):
    """Ohne Ersetzungen kommen exakt dieselben Bytes zurück — inklusive BOM."""
    adapter = TextAdapter()
    segments = adapter.extract_segments(roh)

    assert len(segments) == 1, "text/plain ergibt genau ein Segment"
    assert segments[0].text == TEXT

    assert adapter.apply_replacements(roh, {}) == roh


@pytest.mark.parametrize(
    "roh",
    [
        TEXT.encode("utf-8"),
        BOM + TEXT.encode("utf-8"),
        TEXT.encode("cp1252"),
    ],
    ids=["utf-8", "utf-8-bom", "cp1252"],
)
def test_text_ersetzung_behaelt_encoding(roh):
    """Nach einer Ersetzung bleibt das Encoding erhalten, Umlaute bleiben heil."""
    adapter = TextAdapter()
    segment = adapter.extract_segments(roh)[0]
    start = segment.text.index("Andreas Meier")

    ausgabe = adapter.apply_replacements(
        roh,
        {segment.id: [Replacement(start, start + len("Andreas Meier"), "[[PERSON_1]]")]},
    )

    encoding_vorher, bom_vorher = detect_encoding(roh)
    encoding_nachher, bom_nachher = detect_encoding(ausgabe)
    assert (encoding_nachher, bom_nachher) == (encoding_vorher, bom_vorher)

    erneut = adapter.extract_segments(ausgabe)[0].text
    assert erneut == TEXT.replace("Andreas Meier", "[[PERSON_1]]")
    assert "grüsst aus Zürich" in erneut


def test_text_rechts_nach_links_bei_mehreren_ersetzungen():
    """Zwei Ersetzungen unterschiedlicher Länge in einem Segment."""
    adapter = TextAdapter()
    roh = TEXT.encode("utf-8")
    segment = adapter.extract_segments(roh)[0]

    erste_start = segment.text.index("Andreas Meier")
    zweite_start = segment.text.index("Beat Vogel")
    ausgabe = adapter.apply_replacements(
        roh,
        {
            segment.id: [
                Replacement(erste_start, erste_start + 13, "[[P1]]"),
                Replacement(zweite_start, zweite_start + 10, "[[PERSON_ZWEI]]"),
            ]
        },
    )

    assert adapter.extract_segments(ausgabe)[0].text == (
        "[[P1]] grüsst aus Zürich.\n"
        "[[PERSON_ZWEI]] ist zuständig für Solothurn.\n"
    )


def test_text_strip_metadata_ist_wirkungslos():
    """Textdateien haben keine Metadaten."""
    roh = TEXT.encode("utf-8")
    assert TextAdapter().strip_metadata(roh) == roh


# ── CSV ────────────────────────────────────────────────────────────────────

CSV_TEXT = (
    "Nachname;Vorname;Ort\r\n"
    "Meier;Andreas;Zürich\r\n"
    "Vogel;Beat;Solothurn\r\n"
)


@pytest.mark.parametrize(
    "roh",
    [
        CSV_TEXT.encode("utf-8"),
        BOM + CSV_TEXT.encode("utf-8"),
        CSV_TEXT.encode("cp1252"),
    ],
    ids=["utf-8", "utf-8-bom", "cp1252"],
)
def test_csv_roundtrip_erhaelt_bytes_und_zeilenenden(roh):
    """CRLF bleibt CRLF, BOM bleibt BOM — auch ohne jede Ersetzung."""
    adapter = CsvAdapter()
    segments = adapter.extract_segments(roh)

    assert [s.text for s in segments] == [
        "Nachname;Vorname;Ort",
        "Meier;Andreas;Zürich",
        "Vogel;Beat;Solothurn",
    ]
    assert [s.id for s in segments] == ["csv/0", "csv/1", "csv/2"]
    assert all(s.location.line_ending == "\r\n" for s in segments)

    assert adapter.apply_replacements(roh, {}) == roh


def test_csv_ein_segment_je_zeile_und_gezielte_ersetzung():
    """Eine Ersetzung wirkt genau in ihrer Zeile."""
    adapter = CsvAdapter()
    roh = CSV_TEXT.encode("utf-8")
    segments = {s.id: s for s in adapter.extract_segments(roh)}

    zeile = segments["csv/1"]
    ausgabe = adapter.apply_replacements(
        roh, {zeile.id: [Replacement(0, len("Meier;Andreas"), "[[PERSON_1]]")]}
    )

    assert ausgabe.decode("utf-8") == (
        "Nachname;Vorname;Ort\r\n"
        "[[PERSON_1]];Zürich\r\n"
        "Vogel;Beat;Solothurn\r\n"
    )


def test_csv_letzte_zeile_ohne_umbruch():
    """Fehlt der abschliessende Zeilenumbruch, wird auch keiner erfunden."""
    adapter = CsvAdapter()
    roh = b"a;b\r\nAndreas;Meier"
    segments = adapter.extract_segments(roh)

    assert segments[-1].location.line_ending == ""
    assert adapter.apply_replacements(roh, {}) == roh


def test_csv_leerzeilen_werden_uebersprungen_aber_erhalten():
    """Leere Zeilen liefern kein Segment, bleiben aber in der Datei."""
    adapter = CsvAdapter()
    roh = b"a;b\r\n\r\nAndreas;Meier\r\n"
    segments = adapter.extract_segments(roh)

    assert [s.id for s in segments] == ["csv/0", "csv/2"]
    assert adapter.apply_replacements(roh, {}) == roh


def test_ersetzung_ausserhalb_des_segments_wird_abgelehnt():
    """Ein Offset hinter dem Segmentende ist ein Fehler, keine stille Kürzung."""
    adapter = TextAdapter()
    roh = b"kurz"
    with pytest.raises(ValueError, match="ausserhalb"):
        adapter.apply_replacements(roh, {"text/0": [Replacement(0, 99, "X")]})


# ── Registry ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "eingabe, erwartet",
    [
        ("Bericht.docx", DocxAdapter),
        ("BERICHT.DOCX", DocxAdapter),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            DocxAdapter,
        ),
        ("notizen.txt", TextAdapter),
        ("text/plain", TextAdapter),
        ("text/plain; charset=utf-8", TextAdapter),
        ("export.csv", CsvAdapter),
        ("text/csv", CsvAdapter),
        ("text/csv; charset=utf-8", CsvAdapter),
        ("application/vnd.ms-excel", CsvAdapter),
        ("/tmp/uploads/akte.docx", DocxAdapter),
    ],
)
def test_registry_trifft_den_richtigen_adapter(eingabe, erwartet):
    """MIME-Typ, Dateiname und Pfad führen alle zum passenden Adapter."""
    adapter = get_adapter(eingabe)
    assert type(adapter) is erwartet


def test_registry_csv_vor_text():
    """CSV muss vor text/plain greifen — beide Adapter sind verwandt."""
    assert type(get_adapter("liste.csv")) is CsvAdapter
    assert type(get_adapter("liste.txt")) is TextAdapter


@pytest.mark.parametrize(
    "eingabe, teil_der_meldung",
    [
        ("bericht.pdf", "Phase 5"),
        ("application/pdf", "Phase 5"),
        ("alt.doc", ".docx speichern"),
        ("tabelle.xlsx", "CSV exportieren"),
        ("bild.png", "Nicht unterstütztes Format"),
        ("", "weder ein Dateiname"),
    ],
)
def test_registry_meldet_nicht_unterstuetzte_formate_auf_deutsch(
    eingabe, teil_der_meldung
):
    """Die Fehlermeldung ist deutsch, konkret und nennt bei PDF die Phase."""
    with pytest.raises(UnsupportedFormatError) as fehler:
        get_adapter(eingabe)
    assert teil_der_meldung in str(fehler.value)


def test_unterstuetzte_formate_liste():
    """Phase 1 kennt DOCX, TXT und CSV — und ausdrücklich kein PDF."""
    formate = supported_formats()
    assert ".docx" in formate
    assert ".txt" in formate
    assert ".csv" in formate
    assert ".pdf" not in formate
