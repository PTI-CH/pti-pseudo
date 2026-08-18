"""Tests für den PLZ-Erkenner — die Präzisionsfalle.

Der Wert dieses Erkenners liegt fast vollständig in seinen Negativvektoren.
Ein Erkenner, der ``\\b[1-9]\\d{3}\\b`` bedingungslos meldet, ist trivial zu
schreiben und praktisch unbrauchbar. Die Tests hier belegen, dass die drei
Schranken (Kontext-Gate, Negativfilter, Bereichsprüfung) greifen.
"""

from __future__ import annotations

import pytest

from backend.engine.entities import CH_POSTAL_CODE, Policy, get_policy
from backend.engine.recognizers.plz_ch import (
    BASE_SCORE,
    SwissPostalCodeRecognizer,
    is_plausible_postal_code,
)

#: Texte, in denen eine vierstellige Zahl steht, die **keine** PLZ ist.
NEGATIVE_TEXTS = [
    "im Jahr 2026",
    "Die Verordnung stammt aus dem Jahr 1999.",
    "CHF 8500",
    "Der Betrag von CHF 8500 ist fällig.",
    "Artikel 1234",
    "Gemäss Art. 8001 des Gesetzes.",
    "Seite 1200 des Berichts.",
    "Rechnung 4711 wurde bezahlt.",
    "Die Maschine läuft seit 2019 stabil.",
    "Das Modell 5295 wurde eingestellt.",
]

#: Texte mit echtem Adressbezug.
POSITIVE_TEXTS = [
    ("8001 Zürich, Postleitzahl 8001", "8001"),
    ("Adresse: Bahnhofstrasse 12, 8001 Zürich", "8001"),
    ("Er ist wohnhaft in 3011 Bern.", "3011"),
    ("Wohnort: 6003 Luzern", "6003"),
    ("Zustelladresse lautet 9000 St. Gallen", "9000"),
    ("Domizil in 1201 Genf", "1201"),
    ("c/o Muster AG, 4051 Basel", "4051"),
    ("PLZ 7000 eintragen", "7000"),
    ("Anschrift: Postfach 42, 2000 Neuenburg", "2000"),
]


@pytest.fixture(scope="module")
def recognizer() -> SwissPostalCodeRecognizer:
    return SwissPostalCodeRecognizer()


def analyze(recognizer: SwissPostalCodeRecognizer, text: str):
    return recognizer.analyze(text, [CH_POSTAL_CODE])


# ── Bereichsprüfung ────────────────────────────────────────────────────────


def test_bereich_1000_bis_9999():
    assert is_plausible_postal_code("1000")
    assert is_plausible_postal_code("9999")
    assert not is_plausible_postal_code("0999")
    assert not is_plausible_postal_code("999")
    assert not is_plausible_postal_code("10000")
    assert not is_plausible_postal_code("80a1")


# ── Negativvektoren: das eigentliche Testziel ──────────────────────────────


@pytest.mark.parametrize("text", NEGATIVE_TEXTS)
def test_ohne_adresskontext_kein_treffer(recognizer, text):
    """„im Jahr 2026" und „CHF 8500" dürfen nicht feuern."""
    assert analyze(recognizer, text) == []


def test_jahreszahl_neben_adresse_feuert_nicht(recognizer):
    """Negativfilter schlägt das Kontext-Gate.

    Hier *ist* Adresskontext im Fenster — trotzdem darf die Jahreszahl nicht
    als PLZ gemeldet werden, weil unmittelbar davor „Jahr" steht.
    """
    text = "Adresse unverändert seit dem Jahr 2019."
    assert analyze(recognizer, text) == []


def test_betrag_neben_adresse_feuert_nicht(recognizer):
    text = "Zustelladresse geprüft, Rechnungsbetrag CHF 8500."
    assert analyze(recognizer, text) == []


def test_dreistellige_und_fuenfstellige_zahlen(recognizer):
    """Deutsche PLZ (fünfstellig) und dreistellige Zahlen bleiben unberührt."""
    assert analyze(recognizer, "Adresse: 10115 Berlin") == []
    assert analyze(recognizer, "Adresse: 123 Musterweg") == []


# ── Positivvektoren ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", POSITIVE_TEXTS)
def test_mit_adresskontext_wird_erkannt(recognizer, text, expected):
    results = analyze(recognizer, text)
    assert results, f"Kein Treffer in {text!r}"
    assert all(r.entity_type == CH_POSTAL_CODE for r in results)
    assert text[results[0].start : results[0].end] == expected


def test_geforderter_fall_8001_zuerich(recognizer):
    """Der ausdrücklich geforderte Beweis: mit „Postleitzahl" feuert es."""
    text = "8001 Zürich, Postleitzahl 8001"
    results = analyze(recognizer, text)
    assert len(results) == 2
    assert {text[r.start : r.end] for r in results} == {"8001"}


def test_score_bleibt_niedrig(recognizer):
    """Der Erkenner selbst vergibt höchstens 0.35 — er bleibt eine Vermutung."""
    results = analyze(recognizer, "Wohnort: 6003 Luzern")
    assert results[0].score == pytest.approx(BASE_SCORE)
    assert BASE_SCORE <= 0.35


def test_default_policy_ist_keep():
    """Mehrdeutige Typen werden markiert, nicht ungefragt ersetzt."""
    assert get_policy(CH_POSTAL_CODE) is Policy.KEEP


def test_kontextfenster_ist_begrenzt(recognizer):
    """Ein Adresswort zwei Absätze weiter legitimiert keine Zahl."""
    text = "Adresse: Bahnhofstrasse 12." + " x" * 80 + " Die Zahl 4711 steht hier."
    assert analyze(recognizer, text) == []
