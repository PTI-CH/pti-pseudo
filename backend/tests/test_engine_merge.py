"""Tests für die Merge-/Overlap-Regeln.

Der wichtigste Test ist :func:`test_email_chaos_wird_zu_genau_einem_treffer`:
er bildet den real gemessenen Fall nach, bei dem eine einzige E-Mail-Adresse
vier Treffer erzeugt hat.
"""

from __future__ import annotations

import pytest
from presidio_analyzer import RecognizerResult

from backend.engine import entities
from backend.engine.merge import describe_conflicts, merge_overlaps


def result(entity_type: str, start: int, end: int, score: float) -> RecognizerResult:
    """Kurzschreibweise für einen Treffer."""
    return RecognizerResult(
        entity_type=entity_type, start=start, end=end, score=score
    )


def as_tuples(results):
    return [(r.entity_type, r.start, r.end) for r in results]


# ── Der real beobachtete Fall ──────────────────────────────────────────────

#: Der Text aus dem Spike. Die Offsets unten sind daraus berechnet.
EMAIL_TEXT = "Man erreicht ihn unter andreas.meier@pti.ch oder telefonisch."
EMAIL_START = EMAIL_TEXT.index("andreas.meier@pti.ch")
EMAIL_END = EMAIL_START + len("andreas.meier@pti.ch")


def email_chaos():
    """Die vier Treffer, die Presidio auf dieser Adresse tatsächlich liefert."""
    local_end = EMAIL_TEXT.index("andreas.me") + len("andreas.me")
    domain_start = EMAIL_TEXT.index("pti.ch")
    return [
        result(entities.URL, EMAIL_START, local_end, 0.5),
        result(entities.EMAIL_ADDRESS, EMAIL_START, EMAIL_END, 1.0),
        result(entities.PERSON, EMAIL_START, EMAIL_END, 0.85),
        result(entities.URL, domain_start, domain_start + len("pti.ch"), 0.5),
    ]


def test_email_chaos_wird_zu_genau_einem_treffer():
    """Vier Rohtreffer auf einem Span → genau einer, und zwar EMAIL_ADDRESS."""
    merged = merge_overlaps(email_chaos())

    assert len(merged) == 1
    assert merged[0].entity_type == entities.EMAIL_ADDRESS
    assert (merged[0].start, merged[0].end) == (EMAIL_START, EMAIL_END)
    assert EMAIL_TEXT[merged[0].start : merged[0].end] == "andreas.meier@pti.ch"


def test_email_chaos_ist_unabhaengig_von_der_eingabereihenfolge():
    """Determinismus: gleiche Menge, andere Reihenfolge, gleiches Ergebnis."""
    forward = as_tuples(merge_overlaps(email_chaos()))
    backward = as_tuples(merge_overlaps(list(reversed(email_chaos()))))
    assert forward == backward


def test_begruendung_nennt_den_verdraengenden_treffer():
    lines = describe_conflicts(email_chaos())
    assert len(lines) == 3
    assert all("EMAIL_ADDRESS" in line for line in lines)


# ── Regel 1: höherer Score gewinnt ─────────────────────────────────────────


def test_hoeherer_score_gewinnt_bei_teilueberlappung():
    results = [
        result(entities.PERSON, 0, 10, 0.85),
        result(entities.LOCATION, 5, 15, 0.40),
    ]
    assert as_tuples(merge_overlaps(results)) == [(entities.PERSON, 0, 10)]


def test_hoeherer_score_gewinnt_auch_wenn_er_kuerzer_ist():
    """Score schlägt Länge — die Reihenfolge im Schlüssel ist Absicht."""
    results = [
        result(entities.DATE_TIME, 0, 40, 0.60),
        result(entities.CH_AHV, 10, 26, 1.00),
    ]
    assert as_tuples(merge_overlaps(results)) == [(entities.CH_AHV, 10, 26)]


# ── Regel 2: bei Gleichstand gewinnt der längere Span ──────────────────────


def test_bei_gleichstand_gewinnt_der_laengere_span():
    results = [
        result(entities.LOCATION, 10, 16, 0.85),
        result(entities.ORGANIZATION, 0, 21, 0.85),
    ]
    assert as_tuples(merge_overlaps(results)) == [(entities.ORGANIZATION, 0, 21)]


# ── Regel 3: vollständig enthaltene Treffer entfallen ──────────────────────


def test_enthaltener_treffer_entfaellt_ersatzlos():
    """Nicht gekürzt, nicht zerlegt — der innere Treffer verschwindet."""
    results = [
        result(entities.CH_IBAN, 0, 26, 1.00),
        result(entities.CH_POSTAL_CODE, 10, 14, 0.35),
        result(entities.CH_POSTAL_CODE, 15, 19, 0.35),
    ]
    assert as_tuples(merge_overlaps(results)) == [(entities.CH_IBAN, 0, 26)]


# ── Gleiche Spans ──────────────────────────────────────────────────────────


def test_gleicher_span_gleicher_score_entscheidet_die_vorrangliste():
    """CH_IBAN und IBAN_CODE validieren beide über Mod-97 und landen auf 1.0."""
    results = [
        result(entities.IBAN_CODE, 0, 26, 1.0),
        result(entities.CH_IBAN, 0, 26, 1.0),
    ]
    assert as_tuples(merge_overlaps(results)) == [(entities.CH_IBAN, 0, 26)]


def test_gleicher_span_hoeherer_score_gewinnt():
    # "PHONE_NUMBER" steht bewusst als Literal: Presidios generischer
    # Telefonerkenner liefert diesen Typ, unser Katalog führt ihn nicht.
    results = [
        result("PHONE_NUMBER", 0, 16, 0.40),
        result(entities.CH_PHONE, 0, 16, 1.00),
    ]
    assert as_tuples(merge_overlaps(results)) == [(entities.CH_PHONE, 0, 16)]


# ── Randfälle ──────────────────────────────────────────────────────────────


def test_leere_eingabe():
    assert merge_overlaps([]) == []


def test_beruehrende_spans_ueberlappen_nicht():
    """``a.end == b.start`` teilt kein einziges Zeichen — beide bleiben."""
    results = [
        result(entities.PERSON, 0, 10, 0.85),
        result(entities.LOCATION, 10, 20, 0.85),
    ]
    assert as_tuples(merge_overlaps(results)) == [
        (entities.PERSON, 0, 10),
        (entities.LOCATION, 10, 20),
    ]


def test_disjunkte_treffer_bleiben_alle_erhalten():
    results = [
        result(entities.PERSON, 30, 40, 0.85),
        result(entities.EMAIL_ADDRESS, 0, 20, 1.00),
        result(entities.CH_AHV, 50, 66, 1.00),
    ]
    assert as_tuples(merge_overlaps(results)) == [
        (entities.EMAIL_ADDRESS, 0, 20),
        (entities.PERSON, 30, 40),
        (entities.CH_AHV, 50, 66),
    ]


def test_ergebnis_ist_nach_textposition_sortiert():
    results = [
        result(entities.CH_AHV, 50, 66, 1.00),
        result(entities.PERSON, 30, 40, 0.85),
        result(entities.EMAIL_ADDRESS, 0, 20, 1.00),
    ]
    merged = merge_overlaps(results)
    assert [r.start for r in merged] == sorted(r.start for r in merged)


def test_eingabe_wird_nicht_veraendert():
    results = email_chaos()
    before = as_tuples(results)
    merge_overlaps(results)
    assert as_tuples(results) == before


def test_kette_aus_drei_ueberlappenden_treffern():
    """A überlappt B, B überlappt C, A und C nicht.

    Gewinnt B (höchster Score), fallen A und C weg — obwohl sie sich
    untereinander gar nicht stören. Das ist gewollt: der Text gehört B.
    """
    results = [
        result("A", 0, 10, 0.5),
        result("B", 8, 18, 0.9),
        result("C", 16, 26, 0.5),
    ]
    assert as_tuples(merge_overlaps(results)) == [("B", 8, 18)]


def test_ergebnis_ist_ueberlappungsfrei():
    """Invariante: nach dem Merge teilt kein Paar mehr ein Zeichen."""
    results = [
        result("A", 0, 10, 0.5),
        result("B", 5, 15, 0.5),
        result("C", 12, 20, 0.9),
        result("D", 18, 30, 0.7),
        result("E", 0, 30, 0.4),
    ]
    merged = merge_overlaps(results)
    for first, second in zip(merged, merged[1:]):
        assert first.end <= second.start


@pytest.mark.parametrize("repeats", [2, 5])
def test_wiederholte_laeufe_liefern_dasselbe(repeats):
    results = email_chaos()
    reference = as_tuples(merge_overlaps(results))
    for _ in range(repeats):
        assert as_tuples(merge_overlaps(results)) == reference
