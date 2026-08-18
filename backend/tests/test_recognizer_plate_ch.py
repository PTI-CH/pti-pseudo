"""Tests für den Erkenner Schweizer Kontrollschilder.

Wie bei der PLZ liegt der Wert in den Negativvektoren: das Muster
„zwei Grossbuchstaben plus Zahl" kollidiert mit Adressangaben, Aktenzeichen
und Normbezeichnungen.
"""

from __future__ import annotations

import pytest

from backend.engine.entities import CH_PLATE, Policy, get_policy
from backend.engine.recognizers.plate_ch import (
    BASE_SCORE,
    CANTON_CODES,
    SwissPlateRecognizer,
    is_valid_plate,
)

POSITIVE_TEXTS = [
    ("Das Fahrzeug mit Kontrollschild ZH 123456 wurde kontrolliert.", "ZH 123456"),
    ("Halter des Fahrzeugs BE 4711 ist bekannt.", "BE 4711"),
    ("Kennzeichen: TI 9 an einem Motorrad.", "TI 9"),
    ("Der Personenwagen trägt das Nummernschild GR12345.", "GR12345"),
    ("Beim Strassenverkehrsamt immatrikuliert: VS 88888", "VS 88888"),
]

NEGATIVE_TEXTS = [
    # Kein Fahrzeugbezug — die häufigste Verwechslung überhaupt.
    "ZH 8000 ist eine Adresse",
    "Der Kanton SG 2024 im Vergleich.",
    "Norm SN 1234 gilt weiterhin.",
    # Kleinschreibung: kein Kontrollschild, auch mit Kontext nicht.
    "Das Fahrzeug zh 123456 gibt es nicht.",
    # Kein gültiges Kantonskürzel.
    "Das Fahrzeug XY 123456 gibt es nicht.",
    # Führende Null — so werden Schilder nicht vergeben.
    "Das Fahrzeug ZG 0815 gibt es nicht.",
    # Mehr als sechs Ziffern.
    "Das Fahrzeug ZH 1234567 gibt es nicht.",
]


@pytest.fixture(scope="module")
def recognizer() -> SwissPlateRecognizer:
    return SwissPlateRecognizer()


def analyze(recognizer: SwissPlateRecognizer, text: str):
    return recognizer.analyze(text, [CH_PLATE])


# ── Stammdaten ─────────────────────────────────────────────────────────────


def test_es_gibt_genau_26_kantonskuerzel():
    assert len(CANTON_CODES) == 26
    assert len(set(CANTON_CODES)) == 26


def test_is_valid_plate():
    assert is_valid_plate("ZH 123456")
    assert is_valid_plate("ZH123456")
    assert is_valid_plate("AI 1")
    assert not is_valid_plate("XY 1234")     # kein Kanton
    assert not is_valid_plate("ZG 0815")     # führende Null
    assert not is_valid_plate("ZH 1234567")  # zu lang
    assert not is_valid_plate("ZH")          # ohne Nummer


# ── Positivvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", POSITIVE_TEXTS)
def test_mit_fahrzeugkontext_wird_erkannt(recognizer, text, expected):
    results = analyze(recognizer, text)
    assert len(results) == 1
    assert results[0].entity_type == CH_PLATE
    assert text[results[0].start : results[0].end] == expected


def test_score_bleibt_niedrig(recognizer):
    results = analyze(recognizer, "Halter des Fahrzeugs BE 4711 ist bekannt.")
    assert results[0].score == pytest.approx(BASE_SCORE)
    assert BASE_SCORE <= 0.35


def test_default_policy_ist_keep():
    assert get_policy(CH_PLATE) is Policy.KEEP


# ── Negativvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", NEGATIVE_TEXTS)
def test_negativvektoren(recognizer, text):
    assert analyze(recognizer, text) == []


def test_adresszeile_mit_kantonskuerzel(recognizer):
    """Der im Konzept genannte Kollisionsfall bleibt stumm."""
    assert analyze(recognizer, "Wohnhaft in ZH 8000, Zürich") == []
