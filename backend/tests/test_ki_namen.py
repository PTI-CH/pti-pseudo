"""Tests der Fachlogik der KI-Schicht.

Zwei Dinge werden hier geprueft, und beide sind Zusicherungen und keine
Feinheiten:

1. Die Fenster nehmen **nicht mehr Text mit als vereinbart**. Nur was hier
   entsteht, verlaesst das Haus.
2. Die Antwort **erweitert Fundgrenzen nur**. Ein verkleinerter Fund liesse
   einen Namensteil im Dokument stehen — das gefaehrlichste Fehlerbild dieses
   Werkzeugs, weil das Ergebnis trotzdem pseudonymisiert aussieht.
"""

from __future__ import annotations

from backend.adapters import Segment
from backend.ki.namen import baue_fenster, baue_nachrichten, wende_an
from backend.pipeline import Finding


def _segment(text: str) -> Segment:
    return Segment(id="text", text=text, location=None)


def test_fenster_umfasst_den_fund_und_kontext():
    text = "Die Auskunft erteilte Frau Sandra Zbinden am Montag im Amt."
    start = text.index("Sandra")
    f = Finding("text", "PERSON", start, start + len("Sandra"), 0.85, "Sandra")
    (fenster,) = baue_fenster([_segment(text)], [f], wortradius=3)
    assert "Sandra" in fenster.text
    assert "Zbinden" in fenster.text, "ohne den Nachnamen kann die KI nichts richten"
    assert fenster.markiert == "Sandra"


def test_fenster_bleibt_im_segment():
    """Ueber die Segmentgrenze hinaus geht nichts — auch nicht ein Wort."""
    text = "Sandra Zbinden"
    f = Finding("text", "PERSON", 0, 6, 0.85, "Sandra")
    (fenster,) = baue_fenster([_segment(text)], [f], wortradius=50)
    assert fenster.text == text


def test_wortradius_begrenzt_den_umfang():
    """Der Kern der Datenschutz-Entscheidung: Es geht wirklich nur ein Fenster hinaus."""
    text = " ".join(["wort"] * 40) + " Sandra " + " ".join(["wort"] * 40)
    start = text.index("Sandra")
    f = Finding("text", "PERSON", start, start + 6, 0.85, "Sandra")
    (fenster,) = baue_fenster([_segment(text)], [f], wortradius=5)
    assert len(fenster.text.split()) <= 11
    assert len(fenster.text) < len(text) / 3


def test_nur_person_und_nicht_uebertragene():
    """Uebertragene Funde stammen aus einem anderen, bereits geprueften Fund.

    Sie erneut zu pruefen kostet nur — und wuerde denselben Text ein zweites
    Mal hinausschicken.
    """
    text = "Sandra und die Kantonspolizei Bern"
    findings = [
        Finding("text", "PERSON", 0, 6, 0.85, "Sandra"),
        Finding("text", "ORGANIZATION", 15, 29, 0.85, "Kantonspolizei"),
        Finding("text", "PERSON", 30, 34, 0.85, "Bern", propagated=True),
    ]
    fenster = baue_fenster([_segment(text)], findings, wortradius=3)
    assert len(fenster) == 1
    assert fenster[0].finding_index == 0


def test_nummern_sind_fortlaufend_und_stabil():
    """Die Nummer ist die einzige Adresse, ueber die die Antwort zurueckfindet."""
    text = "Sandra kam. Beat ging."
    findings = [
        Finding("text", "PERSON", 0, 6, 0.85, "Sandra"),
        Finding("text", "PERSON", 12, 16, 0.85, "Beat"),
    ]
    fenster = baue_fenster([_segment(text)], findings, wortradius=2)
    assert [f.nummer for f in fenster] == [0, 1]
    assert [f.finding_index for f in fenster] == [0, 1]


def test_ohne_person_kein_fenster():
    """Nichts gefunden heisst: nichts verlaesst das Haus."""
    text = "Die Kantonspolizei meldet."
    findings = [Finding("text", "ORGANIZATION", 4, 18, 0.85, "Kantonspolizei")]
    assert baue_fenster([_segment(text)], findings) == []


# ── Die Antwort anwenden ───────────────────────────────────────────────────


def _fund(text, wort, propagated=False):
    start = text.index(wort)
    return Finding("text", "PERSON", start, start + len(wort), 0.85, wort, propagated)


def test_grenze_wird_erweitert():
    """Der Zielfall: der Namenssplit verschwindet."""
    text = "Es schrieb Sandra Zbinden am Montag."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings,
        fenster,
        {"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]},
    )
    assert neu[0].text == "Sandra Zbinden"
    assert text[neu[0].start : neu[0].end] == "Sandra Zbinden"
    assert verworfen == 0


def test_verkleinern_wird_verworfen():
    """Ein verkleinerter Fund liesse einen Namensteil im Dokument stehen."""
    text = "Es schrieb Sandra Zbinden am Montag."
    findings = [_fund(text, "Sandra Zbinden")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings,
        fenster,
        {"funde": [{"nummer": 0, "name": "Sandra", "ist_person": True}]},
    )
    assert neu[0].text == "Sandra Zbinden"
    assert verworfen == 1


def test_ist_person_false_wird_ignoriert():
    """Verwerfen darf die KI nicht — aber es wird gezaehlt."""
    text = "Es schrieb Sandra Zbinden am Montag."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings,
        fenster,
        {"funde": [{"nummer": 0, "name": "Sandra", "ist_person": False}]},
    )
    assert len(neu) == 1
    assert neu[0].text == "Sandra"
    assert verworfen == 1


def test_unbekannte_nummer_wird_verworfen():
    text = "Es schrieb Sandra Zbinden."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings,
        fenster,
        {"funde": [{"nummer": 99, "name": "X", "ist_person": True}]},
    )
    assert neu[0].text == "Sandra"
    assert verworfen == 1


def test_name_der_im_fenster_nicht_vorkommt_wird_verworfen():
    """Das Modell darf nichts erfinden."""
    text = "Es schrieb Sandra Zbinden."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings,
        fenster,
        {"funde": [{"nummer": 0, "name": "Ganz Anders", "ist_person": True}]},
    )
    assert neu[0].text == "Sandra"
    assert verworfen == 1


def test_andere_funde_bleiben_unberuehrt():
    text = "Sandra Zbinden und die Kantonspolizei."
    findings = [
        _fund(text, "Sandra"),
        Finding("text", "ORGANIZATION", 23, 37, 0.85, "Kantonspolizei"),
    ]
    fenster = baue_fenster([_segment(text)], findings)
    neu, _ = wende_an(
        findings,
        fenster,
        {"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]},
    )
    assert neu[1].entity_type == "ORGANIZATION"
    assert neu[1].text == "Kantonspolizei"


def test_nachrichten_enthalten_nur_die_fenster():
    """Was nicht im Fenster steht, darf nicht in der Anfrage landen."""
    text = "Geheimer Vorspann. " * 20 + "Es schrieb Sandra Zbinden."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings, wortradius=3)
    nachrichten = baue_nachrichten(fenster)
    inhalt = nachrichten[-1]["content"]
    assert ">>>Sandra<<<" in inhalt
    assert inhalt.count("Geheimer") <= 1, "der Vorspann darf nicht mitgehen"


def test_stueckle_behaelt_die_nummern():
    """Die Nummer ist der Rueckweg zum Fund — Neudurchnummerieren zerrisse ihn."""
    from backend.ki.namen import stueckle

    text = " ".join(f"Name{i}" for i in range(30))
    findings = [
        Finding("text", "PERSON", text.index(f"Name{i}"), text.index(f"Name{i}") + 5, 0.85, f"Name{i}")
        for i in range(25)
    ]
    fenster = baue_fenster([_segment(text)], findings, wortradius=1)
    stuecke = stueckle(fenster, groesse=10)
    assert [len(s) for s in stuecke] == [10, 10, 5]
    alle = [f.nummer for s in stuecke for f in s]
    assert alle == list(range(len(fenster))), "Nummern muessen global bleiben"


def test_stueckle_lehnt_unsinnige_groesse_ab():
    from backend.ki.namen import stueckle

    import pytest

    with pytest.raises(ValueError):
        stueckle([], groesse=0)


def test_schema_verbietet_zusaetzliche_felder():
    """Ohne diese Zeile wird die Antwort unbrauchbar.

    Gemessen am 2026-08-24: Ohne ``additionalProperties: False`` erfindet das
    Modell Felder dazu (rolle, organisation, kontakt, zusammenfassung) und
    braucht dafuer 247 statt 65 Token. Bei mehreren laengeren Fenstern reisst
    damit das Token-Budget, die Antwort wird abgeschnitten — und eine
    abgeschnittene Antwort ist kein gueltiges JSON mehr.
    """
    from backend.ki.namen import SCHEMA

    assert SCHEMA["additionalProperties"] is False
    assert SCHEMA["properties"]["funde"]["items"]["additionalProperties"] is False
