"""Tests für den CH-/LI-IBAN-Erkenner.

Die verwendeten IBAN sind synthetisch: Bankclearing und Kontoteil sind
erfunden, die Prüfziffern nach ISO 7064 dazu berechnet. Sie sind formal
gültig, gehören aber keinem Konto.
"""

from __future__ import annotations

import pytest

from backend.engine.entities import CH_IBAN
from backend.engine.recognizers.iban_ch import (
    SwissIbanRecognizer,
    is_valid_swiss_iban,
    normalize_iban,
)

#: Gültige IBAN in verschiedenen Schreibweisen.
VALID_IBAN = [
    "CH9300762011623852957",
    "CH93 0076 2011 6238 5295 7",
    "CH5604835012345678009",
    "CH5809000000100013377",
    "LI5908800000021234567",
]

#: Jeweils genau ein Zeichen verändert — die Mod-97-Prüfung muss anschlagen.
INVALID_IBAN = [
    "CH9300762011623852958",
    "CH9300762011623852947",
    "CH5604835012345678008",
    "LI5908800000021234568",
]


@pytest.fixture(scope="module")
def recognizer() -> SwissIbanRecognizer:
    return SwissIbanRecognizer()


def analyze(recognizer: SwissIbanRecognizer, text: str):
    return recognizer.analyze(text, [CH_IBAN])


# ── Mod-97-Arithmetik ──────────────────────────────────────────────────────


def test_normalize_entfernt_leerzeichen_und_bindestriche():
    assert normalize_iban("ch93 0076-2011 6238 5295 7") == "CH9300762011623852957"


@pytest.mark.parametrize("iban", VALID_IBAN)
def test_gueltige_pruefsumme(iban):
    assert is_valid_swiss_iban(iban)


@pytest.mark.parametrize("iban", INVALID_IBAN)
def test_ungueltige_pruefsumme(iban):
    assert not is_valid_swiss_iban(iban)


def test_falsche_laenge_ist_ungueltig():
    """20 und 22 Zeichen sind keine CH-IBAN, egal wie die Prüfsumme fällt."""
    assert not is_valid_swiss_iban("CH930076201162385295")
    assert not is_valid_swiss_iban("CH93007620116238529577")


def test_fremdes_land_ist_ungueltig():
    """Eine gültige deutsche IBAN gehört dem generischen Erkenner, nicht uns."""
    assert not is_valid_swiss_iban("DE89370400440532013000")


# ── Positivvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("iban", VALID_IBAN)
def test_gueltige_iban_wird_erkannt(recognizer, iban):
    text = f"Bitte überweisen Sie auf die IBAN {iban} bis Monatsende."
    results = analyze(recognizer, text)

    assert len(results) == 1
    result = results[0]
    assert result.entity_type == CH_IBAN
    assert text[result.start : result.end] == iban
    assert result.score == 1.0


def test_gruppierte_und_kompakte_schreibweise_gleichwertig(recognizer):
    text = (
        "Konto A: CH93 0076 2011 6238 5295 7, "
        "Konto B: CH5604835012345678009."
    )
    results = analyze(recognizer, text)
    assert len(results) == 2
    assert all(r.score == 1.0 for r in results)


def test_liechtensteinische_iban_wird_erkannt(recognizer):
    text = "Zahlungsverbindung: LI5908800000021234567"
    results = analyze(recognizer, text)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "LI5908800000021234567"


# ── Negativvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("iban", INVALID_IBAN)
def test_verfaelschte_iban_wird_nicht_erkannt(recognizer, iban):
    assert analyze(recognizer, f"IBAN {iban}") == []


def test_deutsche_iban_wird_nicht_erkannt(recognizer):
    """22 Zeichen und Ländercode DE — das Muster darf gar nicht greifen."""
    assert analyze(recognizer, "IBAN DE89 3704 0044 0532 0130 00") == []


def test_zu_lange_zeichenkette_wird_nicht_angeschnitten(recognizer):
    """Die Wortgrenze verhindert, dass 22 Zeichen auf 21 gekürzt werden."""
    assert analyze(recognizer, "Referenz CH930076201162385295712345") == []


def test_harmloser_text(recognizer):
    assert analyze(recognizer, "Die Sitzung findet in Chur statt.") == []
