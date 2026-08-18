"""Integrationstest der vollständigen Engine.

Dieser Test lädt das deutsche spaCy-Modell (~400 MB, mehrere Sekunden) und ist
damit der einzige teure Test der Sammlung. Er läuft gegen denselben deutschen
Satz wie der Spike, der die Engine ursprünglich abgenommen hat.

Weil die Engine ein Modul-Singleton ist, kostet das Laden nur einmal pro
Testlauf — alle Tests hier teilen sich dieselbe Instanz.
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.engine import entities
from backend.engine.analyzer import (
    analyze,
    build_analyzer,
    get_default_analyzer,
    supported_entities,
)

#: Der Integrationssatz. Alle Daten darin sind erfunden: der Name, die
#: Firma, die E-Mail-Adresse, die Telefonnummer, die AHV-Nummer (Prüfziffer
#: passend berechnet) und die IBAN (Mod-97 passend berechnet).
TEXT = (
    "Herr Andreas Meier arbeitet bei der PTI Schweiz GmbH in Winterthur. "
    "Sie erreichen ihn unter andreas.meier@pti.ch oder telefonisch unter "
    "+41 44 123 45 67. Seine AHV-Nr. lautet 756.1234.5678.97, das Konto "
    "CH93 0076 2011 6238 5295 7."
)

#: Was im Satz stehen muss — Typ und exakter Textausschnitt.
ERWARTET = {
    entities.PERSON: "Andreas Meier",
    entities.LOCATION: "Winterthur",
    entities.ORGANIZATION: "PTI Schweiz GmbH",
    entities.EMAIL_ADDRESS: "andreas.meier@pti.ch",
    entities.CH_PHONE: "+41 44 123 45 67",
    entities.CH_AHV: "756.1234.5678.97",
    entities.CH_IBAN: "CH93 0076 2011 6238 5295 7",
}


@pytest.fixture(scope="module")
def treffer():
    """Die gemergten Treffer auf dem Integrationssatz."""
    return analyze(TEXT)


@pytest.fixture(scope="module")
def rohtreffer():
    """Die ungefilterten Presidio-Treffer — zum Vergleich mit dem Merge."""
    return analyze(TEXT, merge=False)


# ── Die Kernforderung ──────────────────────────────────────────────────────


def test_jeder_erwartete_typ_genau_einmal(treffer):
    """Person, Ort, Organisation, E-Mail, Telefon, AHV und IBAN je einmal."""
    gezaehlt = Counter(r.entity_type for r in treffer)
    for entity_type in ERWARTET:
        assert gezaehlt[entity_type] == 1, (
            f"{entity_type} wurde {gezaehlt[entity_type]}× gefunden, "
            f"erwartet genau einmal. Gefunden insgesamt: {dict(gezaehlt)}"
        )


@pytest.mark.parametrize("entity_type,ausschnitt", sorted(ERWARTET.items()))
def test_jeder_treffer_deckt_den_richtigen_text_ab(
    treffer, entity_type, ausschnitt
):
    gefunden = [r for r in treffer if r.entity_type == entity_type]
    assert len(gefunden) == 1
    result = gefunden[0]
    assert TEXT[result.start : result.end] == ausschnitt


def test_keine_zusaetzlichen_typen(treffer):
    """Keine Fehltreffer: die Engine findet genau die sieben erwarteten Typen."""
    assert {r.entity_type for r in treffer} == set(ERWARTET)


def test_ergebnis_ist_ueberlappungsfrei(treffer):
    """Die Invariante, die der Anonymisierung zugrunde liegt."""
    for erster, zweiter in zip(treffer, treffer[1:]):
        assert erster.end <= zweiter.start


def test_ergebnis_ist_nach_textposition_sortiert(treffer):
    assert [r.start for r in treffer] == sorted(r.start for r in treffer)


# ── Der Merge macht tatsächlich Arbeit ─────────────────────────────────────


def test_merge_raeumt_mehr_treffer_weg_als_er_behaelt(treffer, rohtreffer):
    """Ohne Merge liefert Presidio auf diesem Satz nachweislich mehr."""
    assert len(rohtreffer) > len(treffer)


def test_email_erzeugt_roh_mehrere_treffer_und_gemergt_genau_einen(
    treffer, rohtreffer
):
    """Der beobachtete Fall, am echten Satz nachgewiesen."""
    start = TEXT.index("andreas.meier@pti.ch")
    ende = start + len("andreas.meier@pti.ch")

    roh_auf_der_adresse = [
        r for r in rohtreffer if r.start < ende and r.end > start
    ]
    assert len(roh_auf_der_adresse) > 1, (
        "Der Testsatz beweist nichts, wenn Presidio hier nur einen "
        "Rohtreffer liefert."
    )

    gemergt = [r for r in treffer if r.start < ende and r.end > start]
    assert len(gemergt) == 1
    assert gemergt[0].entity_type == entities.EMAIL_ADDRESS
    assert (gemergt[0].start, gemergt[0].end) == (start, ende)


def test_spezifischer_iban_erkenner_schlaegt_den_generischen(treffer, rohtreffer):
    """Roh feuern CH_IBAN und IBAN_CODE, gemergt bleibt CH_IBAN."""
    roh_typen = {r.entity_type for r in rohtreffer}
    assert {entities.CH_IBAN, entities.IBAN_CODE} <= roh_typen
    assert entities.IBAN_CODE not in {r.entity_type for r in treffer}


# ── Engine-Aufbau ──────────────────────────────────────────────────────────


def test_engine_ist_ein_singleton():
    """Das Modell darf nie zweimal geladen werden."""
    assert get_default_analyzer() is get_default_analyzer()


def test_engine_beherrscht_die_katalogtypen():
    """Alle Typen aus dem Katalog müssen von der Engine geliefert werden können."""
    koennen = set(supported_entities())
    fehlend = set(entities.CATALOG) - koennen
    assert not fehlend, f"Kein Erkenner liefert: {sorted(fehlend)}"


def test_plz_wird_nur_mit_adresskontext_gefunden():
    """Im Adressblock ja, in der Jahreszahl nein — an der echten Engine."""
    mit_kontext = analyze("Zustelladresse: Bahnhofstrasse 12, 8001 Zürich.")
    assert entities.CH_POSTAL_CODE in {r.entity_type for r in mit_kontext}

    ohne_kontext = analyze("Die Verordnung stammt aus dem Jahr 2026.")
    assert entities.CH_POSTAL_CODE not in {r.entity_type for r in ohne_kontext}


def test_begriffsliste_schlaegt_die_heuristik():
    """Layer 0: ein gepflegter Begriff verdrängt den NER-Treffer darunter."""
    text = "Das Projekt Nachtfalter wurde von Andreas Meier geleitet."
    analyzer = build_analyzer(
        custom_terms=[("Projekt Nachtfalter", entities.ORGANIZATION, "normalized")]
    )
    treffer = analyze(text, analyzer=analyzer)

    organisationen = [
        r for r in treffer if r.entity_type == entities.ORGANIZATION
    ]
    assert len(organisationen) == 1
    assert text[organisationen[0].start : organisationen[0].end] == (
        "Projekt Nachtfalter"
    )
    assert organisationen[0].score == 1.0


def test_analyse_ist_reproduzierbar():
    """Zweimal derselbe Text, zweimal dasselbe Ergebnis — Grundlage des Vaults."""
    erster = [(r.entity_type, r.start, r.end) for r in analyze(TEXT)]
    zweiter = [(r.entity_type, r.start, r.end) for r in analyze(TEXT)]
    assert erster == zweiter


def test_leerer_text():
    assert analyze("") == []
