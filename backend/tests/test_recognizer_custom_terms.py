"""Tests für die gepflegte Begriffsliste (Layer 0).

Wichtigster Punkt neben dem Abgleich selbst: der Erkenner muss **ohne
Datenbank** funktionieren. Kein Test hier baut eine Verbindung auf — die
Liste wird injiziert.
"""

from __future__ import annotations

import pytest

from backend.engine.recognizers.custom_terms import (
    DEFAULT_ENTITY,
    CustomTerm,
    CustomTermsRecognizer,
    MatchMode,
    build_custom_terms_recognizer,
    coerce_term,
)


def analyze(recognizer: CustomTermsRecognizer, text: str, entities=None):
    return recognizer.analyze(text, entities or recognizer.supported_entities)


def spans(recognizer: CustomTermsRecognizer, text: str):
    return [
        (r.entity_type, text[r.start : r.end], r.score)
        for r in analyze(recognizer, text)
    ]


# ── Eingabeformen ──────────────────────────────────────────────────────────


def test_coerce_term_akzeptiert_alle_formen():
    """Die Liste darf als Tupel, Dict, String oder Objekt hereinkommen."""
    assert coerce_term("Nachtfalter") == CustomTerm(
        "Nachtfalter", DEFAULT_ENTITY, MatchMode.EXACT
    )
    assert coerce_term(("Nachtfalter", "ORGANIZATION")) == CustomTerm(
        "Nachtfalter", "ORGANIZATION", MatchMode.EXACT
    )
    assert coerce_term(("Nachtfalter", "ORGANIZATION", "normalized")) == CustomTerm(
        "Nachtfalter", "ORGANIZATION", MatchMode.NORMALIZED
    )
    assert coerce_term(
        {"term": "Nachtfalter", "entity_type": "PERSON", "match_mode": "exact"}
    ) == CustomTerm("Nachtfalter", "PERSON", MatchMode.EXACT)


def test_leerer_begriff_wird_abgelehnt():
    with pytest.raises(ValueError):
        CustomTerm("   ")


def test_unbekannter_match_mode_wird_abgelehnt():
    with pytest.raises(ValueError):
        CustomTerm("Nachtfalter", "PERSON", "fuzzy")


def test_unbekannte_eingabeform_wird_abgelehnt():
    with pytest.raises(TypeError):
        coerce_term(42)


def test_leere_liste_funktioniert():
    """Ohne Begriffe darf nichts knallen — und nichts gefunden werden."""
    recognizer = build_custom_terms_recognizer([])
    assert recognizer.supported_entities == [DEFAULT_ENTITY]
    assert analyze(recognizer, "Irgendein Text") == []


def test_ohne_argument_funktioniert():
    assert analyze(build_custom_terms_recognizer(), "Irgendein Text") == []


# ── Modus exact ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def exact_recognizer() -> CustomTermsRecognizer:
    return build_custom_terms_recognizer(
        [
            ("KKPKS", "ORGANIZATION", "exact"),
            ("Projekt Nachtfalter", "ORGANIZATION", "exact"),
        ]
    )


def test_exact_trifft_die_genaue_schreibweise(exact_recognizer):
    text = "Die KKPKS hat das Projekt Nachtfalter genehmigt."
    assert spans(exact_recognizer, text) == [
        ("ORGANIZATION", "KKPKS", 1.0),
        ("ORGANIZATION", "Projekt Nachtfalter", 1.0),
    ]


def test_exact_ist_gross_klein_empfindlich(exact_recognizer):
    """Genau das unterscheidet ``exact`` von ``normalized``."""
    assert analyze(exact_recognizer, "Siehe kkpks.example.com für Details.") == []
    assert analyze(exact_recognizer, "das projekt nachtfalter") == []


def test_exact_achtet_auf_wortgrenzen(exact_recognizer):
    assert analyze(exact_recognizer, "KKPKSVERBUND ist etwas anderes.") == []
    assert analyze(exact_recognizer, "XKKPKS ist etwas anderes.") == []


# ── Modus normalized ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def normalized_recognizer() -> CustomTermsRecognizer:
    return build_custom_terms_recognizer(
        [
            ("Projekt Nachtfalter", "ORGANIZATION", "normalized"),
            ("Zürcher Fachstelle", "ORGANIZATION", "normalized"),
        ]
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Projekt Nachtfalter", "Projekt Nachtfalter"),
        ("projekt nachtfalter", "projekt nachtfalter"),
        ("PROJEKT NACHTFALTER", "PROJEKT NACHTFALTER"),
        ("Projekt   Nachtfalter", "Projekt   Nachtfalter"),
        ("Projekt\nNachtfalter", "Projekt\nNachtfalter"),
    ],
)
def test_normalized_ist_schreibweisen_tolerant(normalized_recognizer, text, expected):
    full = f"Betrifft {text} im Anhang."
    results = analyze(normalized_recognizer, full)
    assert len(results) == 1
    assert full[results[0].start : results[0].end] == expected


@pytest.mark.parametrize(
    "written", ["Zürcher Fachstelle", "Zuercher Fachstelle", "zürcher fachstelle"]
)
def test_normalized_faltet_umlaute_in_beide_richtungen(
    normalized_recognizer, written
):
    results = analyze(normalized_recognizer, f"Die {written} meldet sich.")
    assert len(results) == 1


def test_normalized_liefert_offsets_in_den_originaltext(normalized_recognizer):
    """Der Grund, warum das Muster gefaltet wird und nicht der Text.

    Würde für den Abgleich der Text normalisiert (``ü`` → ``ue``), verschöben
    sich alle Offsets dahinter um ein Zeichen — und die Anonymisierung
    ersetzte die falschen Stellen.
    """
    text = "Vorwort über die Zürcher Fachstelle und mehr."
    results = analyze(normalized_recognizer, text)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "Zürcher Fachstelle"


def test_normalized_achtet_trotzdem_auf_wortgrenzen(normalized_recognizer):
    assert analyze(normalized_recognizer, "XProjekt NachtfalterX") == []


# ── Zusammenspiel ──────────────────────────────────────────────────────────


def test_gemischte_entitaetstypen_in_einer_liste():
    recognizer = build_custom_terms_recognizer(
        [
            ("Nachtfalter", "ORGANIZATION", "exact"),
            ("Der Adler", "PERSON", "normalized"),
            ("XR-9", "CUSTOM_TERM", "exact"),
        ]
    )
    assert recognizer.supported_entities == [
        "CUSTOM_TERM",
        "ORGANIZATION",
        "PERSON",
    ]

    text = "Nachtfalter wurde von der adler mit XR-9 gemeldet."
    found = {entity for entity, _, _ in spans(recognizer, text)}
    assert found == {"ORGANIZATION", "PERSON", "CUSTOM_TERM"}


def test_entities_filter_wird_beachtet():
    """Fragt der Aufrufer nur nach einem Typ, kommt auch nur dieser zurück."""
    recognizer = build_custom_terms_recognizer(
        [("Nachtfalter", "ORGANIZATION"), ("Adler", "PERSON")]
    )
    text = "Nachtfalter und Adler."
    results = analyze(recognizer, text, entities=["PERSON"])
    assert [r.entity_type for r in results] == ["PERSON"]


def test_score_ist_eins():
    """Eine ausdrücklich gepflegte Liste schlägt jede Heuristik."""
    recognizer = build_custom_terms_recognizer([("Nachtfalter", "ORGANIZATION")])
    assert analyze(recognizer, "Nachtfalter")[0].score == 1.0


def test_sonderzeichen_im_begriff():
    """``\\b`` würde hier versagen — deshalb die Lookaround-Wortgrenzen."""
    recognizer = build_custom_terms_recognizer([("§14a-Verfahren", "CUSTOM_TERM")])
    text = "Das §14a-Verfahren ist eröffnet."
    results = analyze(recognizer, text)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "§14a-Verfahren"
