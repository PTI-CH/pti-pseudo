"""Tests für den AHV-Erkenner.

Die Testnummern sind **synthetisch**: die neun mittleren Ziffern sind frei
erfunden, die Prüfziffer ist dazu passend berechnet. Sie sind formal gültig,
aber niemandem zugeteilt — echte Personendaten gehören nach Hausregel nicht
ins Repo, auch nicht in Fixtures.
"""

from __future__ import annotations

import pytest

from backend.engine.entities import CH_AHV
from backend.engine.recognizers.ahv import (
    SwissAhvRecognizer,
    compute_check_digit,
    format_ahv,
    is_valid_ahv,
)

#: Gültige Nummern, Prüfziffer jeweils selbst nach EAN-13 berechnet.
#: Kontrollrechnung für die erste: 7·1+5·3+6·1+1·3+2·1+3·3+4·1+5·3+6·1+7·3+
#: 8·1+9·3 = 123 → (10 − 3) mod 10 = 7.
VALID_AHV = [
    "756.1234.5678.97",
    "756.9876.5432.17",
    "756.0000.1112.27",
    "756.3456.7891.25",
    "756.1111.2222.39",
]

#: Dieselben Nummern mit **falscher Prüfziffer** — sonst identisch. Genau
#: dieser Unterschied muss den Treffer verhindern; sonst wäre die
#: Prüfziffernprüfung wirkungslos.
INVALID_CHECK_DIGIT = [
    "756.1234.5678.98",
    "756.9876.5432.16",
    "756.0000.1112.20",
    "756.3456.7891.24",
    "756.1111.2222.31",
]


@pytest.fixture(scope="module")
def recognizer() -> SwissAhvRecognizer:
    """Der Erkenner. Braucht kein Sprachmodell und ist daher billig."""
    return SwissAhvRecognizer()


def analyze(recognizer: SwissAhvRecognizer, text: str):
    """Kleiner Helfer: Erkenner direkt ohne AnalyzerEngine anwerfen."""
    return recognizer.analyze(text, [CH_AHV])


# ── Prüfziffernarithmetik ──────────────────────────────────────────────────


def test_pruefziffer_der_dokumentierten_nummer():
    """Die im Fachkonzept genannte Nummer muss die Prüfziffer 7 ergeben."""
    assert compute_check_digit("756123456789") == 7


@pytest.mark.parametrize("number", VALID_AHV)
def test_pruefziffer_stimmt_bei_gueltigen_nummern(number):
    digits = number.replace(".", "")
    assert compute_check_digit(digits[:12]) == int(digits[12])


def test_pruefziffer_verlangt_zwoelf_ziffern():
    with pytest.raises(ValueError):
        compute_check_digit("7561234")


def test_is_valid_ahv_ignoriert_trennzeichen():
    """Punktiert, kompakt und mit Leerzeichen ist dieselbe Nummer."""
    assert is_valid_ahv("756.1234.5678.97")
    assert is_valid_ahv("7561234567897")
    assert is_valid_ahv("756 1234 5678 97")


def test_is_valid_ahv_verlangt_laenderpraefix():
    """Gleiche Länge, gleiche Prüfziffernlogik, falsches Präfix → ungültig."""
    assert not is_valid_ahv("7571234567897")


def test_is_valid_ahv_verlangt_dreizehn_ziffern():
    assert not is_valid_ahv("756123456789")
    assert not is_valid_ahv("75612345678970")


def test_format_ahv():
    assert format_ahv("7561234567897") == "756.1234.5678.97"


# ── Positivvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("number", VALID_AHV)
def test_gueltige_nummer_wird_erkannt(recognizer, number):
    text = f"Die AHV-Nummer der versicherten Person lautet {number}."
    results = analyze(recognizer, text)

    assert len(results) == 1
    result = results[0]
    assert result.entity_type == CH_AHV
    assert text[result.start : result.end] == number
    # Bestandene Prüfziffer → volle Konfidenz, nicht der Musterscore.
    assert result.score == 1.0


def test_kompakte_schreibweise_wird_erkannt(recognizer):
    text = "Import aus dem CSV: 7561234567897;Meier;Andreas"
    results = analyze(recognizer, text)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "7561234567897"


def test_schreibweise_mit_leerzeichen_wird_erkannt(recognizer):
    text = "Versichertennummer 756 1234 5678 97 gemäss Ausweis."
    results = analyze(recognizer, text)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "756 1234 5678 97"


def test_mehrere_nummern_im_selben_text(recognizer):
    text = (
        "Erste Person: 756.1234.5678.97, zweite Person: 756.9876.5432.17, "
        "dritte Person: 756.0000.1112.27."
    )
    results = analyze(recognizer, text)
    assert len(results) == 3
    assert all(r.score == 1.0 for r in results)


# ── Negativvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("number", INVALID_CHECK_DIGIT)
def test_falsche_pruefziffer_wird_nicht_erkannt(recognizer, number):
    """Der eigentliche Beweis: nur die Prüfziffer unterscheidet sich."""
    assert not is_valid_ahv(number)
    assert analyze(recognizer, f"AHV-Nummer {number}") == []


def test_falsches_laenderpraefix_wird_nicht_erkannt(recognizer):
    """757 ist kein Schweizer Präfix — das Muster darf gar nicht greifen."""
    assert analyze(recognizer, "Nummer 757.1234.5678.97") == []


def test_teil_einer_laengeren_ziffernkette_wird_nicht_erkannt(recognizer):
    """Wortgrenzen: eine AHV-Nummer wird nicht aus einer Referenz geschnitten."""
    assert analyze(recognizer, "Referenz 99756123456789712345") == []


def test_zu_kurze_und_zu_lange_nummern(recognizer):
    assert analyze(recognizer, "756.1234.5678.9") == []
    assert analyze(recognizer, "756.1234.5678.977") == []


def test_leerer_und_harmloser_text(recognizer):
    assert analyze(recognizer, "") == []
    assert analyze(recognizer, "Im Jahr 2026 kostete es 8500 Franken.") == []
