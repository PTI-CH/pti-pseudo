"""Tests der Wortart-Regel.

Die Regel verwirft Funde. In einem Werkzeug, das Personendaten entfernt, ist
das die gefaehrlichste Sorte Code, die es gibt — deshalb pruefen die meisten
Tests hier, was sie **nicht** anfassen darf.
"""

from __future__ import annotations

import pytest

from backend.engine.analyzer import analyze
from backend.engine.entities import LOCATION, ORGANIZATION, PERSON
from backend.engine.nlp import LANGUAGE, get_nlp_engine
from backend.engine.wortarten import SPACY_RECOGNIZER, verwerfe_gattungswoerter

pytest.importorskip("presidio_analyzer")
from presidio_analyzer import RecognizerResult  # noqa: E402


@pytest.fixture(scope="module")
def doc_von():
    """Baut ein spaCy-Doc — einmal je Modul, das Modell kostet Sekunden."""
    engine = get_nlp_engine()

    def bauen(text: str):
        return engine.process_text(text, LANGUAGE).tokens

    return bauen


def _spacy_fund(entity_type: str, start: int, ende: int) -> RecognizerResult:
    """Ein Treffer, wie ihn der spaCy-Erkenner liefert."""
    return RecognizerResult(
        entity_type=entity_type,
        start=start,
        end=ende,
        score=0.85,
        recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: SPACY_RECOGNIZER},
    )


# ── Was die Regel verwirft ─────────────────────────────────────────────────


def test_einzelnes_gattungswort_wird_verworfen(doc_von) -> None:
    """``Lagebericht`` ist ein Substantiv, kein Personenname."""
    text = "Lagebericht"
    behalten, verworfen = verwerfe_gattungswoerter(
        [_spacy_fund(PERSON, 0, len(text))], doc_von(text)
    )
    assert behalten == []
    assert len(verworfen) == 1


# ── Was sie unberuehrt lassen muss ─────────────────────────────────────────


def test_eigenname_bleibt(doc_von) -> None:
    """Ein einzelnes Token, das das Modell als PROPN fuehrt, ist ein Name."""
    text = "Sandra"
    behalten, verworfen = verwerfe_gattungswoerter(
        [_spacy_fund(PERSON, 0, len(text))], doc_von(text)
    )
    assert len(behalten) == 1
    assert verworfen == []


def test_mehrwortfund_bleibt_auch_wenn_ein_teil_substantiv_ist(doc_von) -> None:
    """Die Regel gilt nur fuer Einwortfunde — sonst wuerde sie zu breit.

    Bei ``Andrea Beispiel`` ist die Sache klar, und dort liegt der Grossteil
    der echten Namen.
    """
    text = "Andrea Beispiel"
    behalten, verworfen = verwerfe_gattungswoerter(
        [_spacy_fund(PERSON, 0, len(text))], doc_von(text)
    )
    assert len(behalten) == 1
    assert verworfen == []


def test_fund_ueber_eine_zellgrenze_gilt_nicht_als_einwortfund(doc_von) -> None:
    """``Zwicky\\tMarlene`` sind zwei Woerter, getrennt durch den Tabulator.

    Der XLSX-Adapter verbindet die Zellen einer Zeile mit Tabulator. spaCy
    fuehrt den als eigenes SPACE-Token — zaehlte es mit, waere der Fund
    dreitokenig; zaehlte man die Tokens naiv, koennte ein zweiteiliger Name
    faelschlich als Einwortfund gelten und der Regel zum Opfer fallen.
    """
    text = "Zwicky\tMarlene"
    behalten, verworfen = verwerfe_gattungswoerter(
        [_spacy_fund(PERSON, 0, len(text))], doc_von(text)
    )
    assert len(behalten) == 1
    assert verworfen == []


def test_organisation_und_ort_bleiben_unberuehrt(doc_von) -> None:
    """Die Regel gilt nur fuer PERSON.

    Organisations- und Ortsnamen sind im Deutschen ueberwiegend aus
    gewoehnlichen Woertern gebaut — ``Talblick``, ``Bahnhofstrasse``. Eine
    Fassung der Regel, die auch sie erfasste, kostete am Golden-Set sieben
    echte Funde; die Begruendung steht im Kopf von ``engine/wortarten.py``.
    """
    for typ, text in ((ORGANIZATION, "Talblick"), (LOCATION, "Bahnhofstrasse")):
        behalten, verworfen = verwerfe_gattungswoerter(
            [_spacy_fund(typ, 0, len(text))], doc_von(text)
        )
        assert len(behalten) == 1, f"{typ} {text!r} darf nicht verworfen werden"
        assert verworfen == []


def test_begriffsliste_wird_nie_angetastet(doc_von) -> None:
    """Ein Listeneintrag ist die Anordnung eines Menschen.

    Er kommt nicht vom spaCy-Erkenner und faellt deshalb gar nicht erst unter
    die Regel — auch dann nicht, wenn das Wort ein Substantiv ist.
    """
    text = "Lagebericht"
    aus_der_liste = RecognizerResult(
        entity_type=PERSON,
        start=0,
        end=len(text),
        score=1.0,
        recognition_metadata={
            RecognizerResult.RECOGNIZER_NAME_KEY: "CustomTermsRecognizer"
        },
    )
    behalten, verworfen = verwerfe_gattungswoerter([aus_der_liste], doc_von(text))
    assert len(behalten) == 1
    assert verworfen == []


def test_ohne_doc_bleibt_alles_erhalten() -> None:
    """Ohne Wortarten wird nicht geraten.

    Ein verworfener Name ist teurer als ein Fehlalarm — faellt die
    NLP-Schicht aus, laeuft die Erkennung lieber wie vorher weiter.
    """
    funde = [_spacy_fund(PERSON, 0, 5)]
    behalten, verworfen = verwerfe_gattungswoerter(funde, None)
    assert behalten == funde
    assert verworfen == []


# ── Der Weg durch die ganze Engine ─────────────────────────────────────────


@pytest.mark.parametrize("name", ["Bauer", "Koch", "Sommer", "Jäger", "Meier"])
def test_mehrdeutiger_nachname_im_satz_ist_ein_eigenname(doc_von, name: str) -> None:
    """Der wichtigste Fall: im Fliesstext darf die Regel nichts anfassen.

    Viele deutsche Nachnamen *sind* Substantive. Im Satz fuehrt das Modell sie
    aber als ``PROPN`` — nachgemessen am 2026-08-24 an 20 solchen Namen, 0
    Fehler. Damit greift die Regel dort nie, unabhaengig davon, ob das
    NER-Modell den Namen ueberhaupt meldet.

    Genau diese Unterscheidung ist der Punkt: ``Bauer`` und ``Sommer`` findet
    das Modell in diesem Satz gar nicht als PERSON — eine bestehende
    Recall-Luecke, die es vor dieser Regel schon gab. Der Test darf ihr also
    nicht die Schuld geben und prueft deshalb die Wortart, nicht den Fund.
    """
    satz = f"Die Auskunft erteilte Frau {name} am Montag."
    token = [t for t in doc_von(satz) if t.text == name][0]
    assert token.pos_ == "PROPN", (
        f"{name!r} gilt im Satz als {token.pos_} — die Regel wuerde einen "
        "echten Namen verwerfen"
    )


def test_gefundener_name_im_satz_ueberlebt_die_regel() -> None:
    """Und wo das Modell den Namen meldet, kommt er auch durch die Engine."""
    for name in ("Koch", "Jäger"):
        treffer = analyze(f"Die Auskunft erteilte Frau {name} am Montag.")
        personen = [t.entity_type for t in treffer if t.entity_type == PERSON]
        assert personen, f"{name!r} im Satz wurde verworfen — Regel zu breit"
