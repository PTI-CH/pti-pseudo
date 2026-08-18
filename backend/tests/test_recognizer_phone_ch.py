"""Tests für den Schweizer Telefon-Erkenner.

Alle Nummern sind erfunden. Die Gebietskennzahlen sind echt — sonst würde der
Test die Vorwahlprüfung nicht wirklich prüfen —, die Teilnehmernummern nicht.
"""

from __future__ import annotations

import pytest

from backend.engine.entities import CH_PHONE
from backend.engine.recognizers.phone_ch import (
    SwissPhoneRecognizer,
    is_valid_swiss_phone,
    to_national_number,
)

#: Dieselbe Nummer in allen Schreibweisen, die vorkommen dürfen.
VALID_FORMS = [
    "+41 44 123 45 67",
    "+41441234567",
    "+41-44-123-45-67",
    "+41.44.123.45.67",
    "+41 (0)44 123 45 67",
    "0041 44 123 45 67",
    "0041441234567",
    "41 44 123 45 67",
    "044 123 45 67",
    "044/123 45 67",
    "044.123.45.67",
    "044-123-45-67",
    "0441234567",
]

#: Je eine Nummer aus jedem abgedeckten Bereich.
VALID_AREA_CODES = [
    "022 123 45 67",   # Genf
    "027 123 45 67",   # Wallis
    "031 123 45 67",   # Bern
    "041 123 45 67",   # Luzern
    "043 123 45 67",   # Zürich
    "052 123 45 67",   # Winterthur
    "058 123 45 67",   # Unternehmensnetze
    "061 123 45 67",   # Basel
    "071 123 45 67",   # St. Gallen
    "081 123 45 67",   # Graubünden
    "091 123 45 67",   # Tessin
    "076 123 45 67",   # Mobil
    "079 123 45 67",   # Mobil
    "0800 123 456",    # Gratisnummer
    "0848 800 100",    # Business-Nummer
    "0900 123 456",    # Mehrwertdienst
]

#: Formal richtig gebaut, aber der Nummernraum kennt diese Bereiche nicht.
INVALID_AREA_CODES = [
    "+41 99 123 45 67",
    "+41 10 123 45 67",
    "+41 35 123 45 67",
    "+41 68 123 45 67",
    "093 123 45 67",
    # 855 ist keine Dienstenummer und 85 kein Festnetzbereich.
    # (Achtung: 0810… wäre *gültig* — das ist 081 aus Graubünden mit einer
    # Teilnehmernummer, die mit 0 beginnt.)
    "0855 123 456",
    "0866 123 456",
]


@pytest.fixture(scope="module")
def recognizer() -> SwissPhoneRecognizer:
    return SwissPhoneRecognizer()


def analyze(recognizer: SwissPhoneRecognizer, text: str):
    return recognizer.analyze(text, [CH_PHONE])


# ── Normalisierung ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("number", VALID_FORMS)
def test_alle_schreibweisen_ergeben_dieselbe_nsn(number):
    """Der Kern des Erkenners: jede Form muss auf 441234567 zusammenfallen."""
    assert to_national_number(number) == "441234567"


def test_zu_kurze_und_zu_lange_nummern_haben_keine_nsn():
    assert to_national_number("044 123 45") is None
    assert to_national_number("044 123 45 67 89") is None


# ── Positivvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("number", VALID_FORMS)
def test_jede_schreibweise_wird_erkannt(recognizer, number):
    text = f"Sie erreichen uns unter {number} während der Bürozeiten."
    results = analyze(recognizer, text)

    assert len(results) == 1
    result = results[0]
    assert result.entity_type == CH_PHONE
    assert text[result.start : result.end] == number
    assert result.score == 1.0


@pytest.mark.parametrize("number", VALID_AREA_CODES)
def test_gueltige_vorwahlen_werden_erkannt(recognizer, number):
    assert is_valid_swiss_phone(number)
    results = analyze(recognizer, f"Telefon: {number}")
    assert len(results) == 1


def test_mehrere_nummern_im_selben_text(recognizer):
    text = "Zentrale 044 123 45 67, Mobile +41 79 987 65 43, Fax 044/123 45 68."
    results = analyze(recognizer, text)
    assert len(results) == 3


# ── Negativvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("number", INVALID_AREA_CODES)
def test_ungueltige_vorwahl_wird_nicht_erkannt(recognizer, number):
    """Der geforderte Fall: +41 99 123 45 67 gibt es im Nummernraum nicht."""
    assert not is_valid_swiss_phone(number)
    assert analyze(recognizer, f"Telefon: {number}") == []


def test_auslaendische_nummer_wird_nicht_erkannt(recognizer):
    """Deutsche Nummer: falscher Ländercode, falsche Länge."""
    assert analyze(recognizer, "Telefon +49 30 123456") == []


def test_ahv_nummer_wird_nicht_als_telefon_erkannt(recognizer):
    """Ziffernblöcke mit Punkten sind ein realer Anlass für Fehltreffer."""
    assert analyze(recognizer, "AHV-Nummer 756.0000.1112.27") == []


def test_teil_einer_laengeren_ziffernkette_wird_nicht_erkannt(recognizer):
    assert analyze(recognizer, "Beleg 12300441234567890") == []


def test_zu_kurze_nummer_wird_nicht_erkannt(recognizer):
    assert analyze(recognizer, "Intern: 044 123 45") == []


def test_harmloser_text(recognizer):
    assert analyze(recognizer, "Die Sitzung dauert von 9 bis 17 Uhr.") == []
