"""Tests fuer das feste Pseudonym eines Begriffs (``CustomTerm.replacement``).

Das Feld gab es in der Oberflaeche schon — es wurde nur nirgends ausgewertet:
``TermCreate`` kannte es nicht, die Tabelle hatte keine Spalte dafuer, und der
Wert fiel still unter den Tisch. Wer ihn setzte, bekam trotzdem ein gezaehltes
``[[ORGANIZATION_3]]``.

Der heikle Teil ist nicht das Einsetzen, sondern die **Rueckfuehrung**: ein
festes Pseudonym muss im Vault stehen, sonst findet ``documents:reverse`` sein
Symbol nicht wieder. Und es muss je Vault eindeutig bleiben — zwei Klartexte
hinter demselben Symbol waeren nach der Rueckfuehrung nicht mehr zu trennen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters.base import Segment  # noqa: E402
from backend.pipeline import Finding, build_replacements, feste_pseudonyme  # noqa: E402

#: Frei erfunden.
LIEFERANT = "Talblick"
TEXT = f"Der Vertrag mit {LIEFERANT} laeuft weiter."


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "d" * 64)

    from backend.config import get_settings
    from backend.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()

    from backend.main import create_app

    init_db()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()


# ── Aufloesung ohne Datenbank ──────────────────────────────────────────────


def _fund(text: str = LIEFERANT, entity_type: str = "ORGANIZATION") -> Finding:
    return Finding(
        segment_id="text", entity_type=entity_type, start=0, end=len(text),
        score=1.0, text=text,
    )


def test_festes_pseudonym_wird_eingesetzt():
    begriffe = [
        {
            "term": LIEFERANT,
            "entity_type": "ORGANIZATION",
            "match_mode": "normalized",
            "replacement": "[[LIEFERANT_A]]",
        }
    ]

    _, mapping = build_replacements(None, None, [_fund()], begriffe)

    assert mapping == {LIEFERANT: "[[LIEFERANT_A]]"}


def test_ohne_festes_pseudonym_bleibt_es_beim_typ():
    begriffe = [
        {"term": LIEFERANT, "entity_type": "ORGANIZATION", "match_mode": "exact"}
    ]

    _, mapping = build_replacements(None, None, [_fund()], begriffe)

    assert mapping == {LIEFERANT: "[[ORGANIZATION]]"}


def test_anderer_typ_bekommt_das_pseudonym_nicht():
    """Der Begriff gilt fuer seinen Typ, nicht fuer denselben Text in jedem Typ."""
    begriffe = [
        {
            "term": LIEFERANT,
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "replacement": "[[LIEFERANT_A]]",
        }
    ]

    _, mapping = build_replacements(
        None, None, [_fund(entity_type="PERSON")], begriffe
    )

    assert mapping == {LIEFERANT: "[[PERSON]]"}


def test_laengerer_fund_bekommt_das_pseudonym_nicht():
    """``Talblick AG`` ist nicht ``Talblick`` — sonst stuende dort ein Symbol,
    das fuer einen anderen Klartext steht."""
    begriffe = [
        {
            "term": LIEFERANT,
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "replacement": "[[LIEFERANT_A]]",
        }
    ]

    _, mapping = build_replacements(
        None, None, [_fund(text=f"{LIEFERANT} AG")], begriffe
    )

    assert mapping == {f"{LIEFERANT} AG": "[[ORGANIZATION]]"}


def test_normalized_greift_auch_bei_anderer_schreibweise():
    begriffe = [
        {
            "term": "Zürich Versicherung",
            "entity_type": "ORGANIZATION",
            "match_mode": "normalized",
            "replacement": "[[VERSICHERER]]",
        }
    ]

    _, mapping = build_replacements(
        None, None, [_fund(text="zuerich versicherung")], begriffe
    )

    assert mapping == {"zuerich versicherung": "[[VERSICHERER]]"}


def test_tabelle_ist_deterministisch_sortiert():
    begriffe = [
        {"term": "B", "entity_type": "ORGANIZATION", "replacement": "[[Z]]"},
        {"term": "A", "entity_type": "ORGANIZATION", "replacement": "[[A]]"},
        {"term": "C", "entity_type": "ORGANIZATION"},
    ]

    tabelle = feste_pseudonyme(begriffe)

    assert [eintrag[2] for eintrag in tabelle] == ["[[A]]", "[[Z]]"]


# ── Ueber die REST-Schnittstelle, mit Vault und Rueckfuehrung ──────────────


def _lege_begriff_an(client: TestClient, term: str, ersatz: str | None) -> None:
    r = client.post(
        "/api/v1/terms",
        json={
            "term": term,
            "entity_type": "ORGANIZATION",
            "match_mode": "normalized",
            "replacement": ersatz,
        },
    )
    assert r.status_code == 201, r.text


def test_rest_speichert_und_liefert_das_feste_pseudonym(client: TestClient) -> None:
    _lege_begriff_an(client, "Seeblick", "[[LIEFERANT_S]]")

    eintraege = client.get("/api/v1/terms").json()["terms"]
    treffer = [t for t in eintraege if t["term"] == "Seeblick"]

    assert treffer and treffer[0]["replacement"] == "[[LIEFERANT_S]]"


def test_rueckfuehrung_findet_das_feste_pseudonym_wieder(client: TestClient) -> None:
    """Der eigentliche Punkt: fest ersetzt heisst nicht unumkehrbar."""
    _lege_begriff_an(client, "Bergblick", "[[LIEFERANT_B]]")
    text = "Der Vertrag mit Bergblick laeuft weiter."

    hin = client.post(
        "/api/v1/documents:anonymize",
        json={"text": text, "mode": "reversible", "entities": ["ORGANIZATION"]},
    )
    assert hin.status_code == 200, hin.text
    assert "[[LIEFERANT_B]]" in hin.json()["text"]
    assert "Bergblick" not in hin.json()["text"]

    zurueck = client.post(
        "/api/v1/documents:reverse",
        json={"text": hin.json()["text"], "vault_id": hin.json()["vault_id"]},
    )
    assert zurueck.status_code == 200, zurueck.text
    assert zurueck.json()["text"] == text


def test_leeres_feld_bedeutet_kein_festes_pseudonym(client: TestClient) -> None:
    r = client.post(
        "/api/v1/terms",
        json={
            "term": "Feldblick",
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "replacement": "   ",
        },
    )

    assert r.status_code == 201, r.text
    assert r.json()["replacement"] is None


def test_doppeltes_festes_pseudonym_wird_abgelehnt(client: TestClient) -> None:
    """Zwei Klartexte hinter demselben Symbol waeren nicht mehr zu trennen."""
    _lege_begriff_an(client, "Waldblick", "[[LIEFERANT_W]]")

    r = client.post(
        "/api/v1/terms",
        json={
            "term": "Wiesenblick",
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "replacement": "[[LIEFERANT_W]]",
        },
    )

    assert r.status_code == 409
    assert "Waldblick" in r.json()["detail"]


def test_zu_langes_pseudonym_wird_abgelehnt(client: TestClient) -> None:
    """Ueber 100 Zeichen schluege sonst erst die Datenbank zu — mitten im Lauf."""
    r = client.post(
        "/api/v1/terms",
        json={
            "term": "Langblick",
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "replacement": "X" * 101,
        },
    )

    assert r.status_code == 422


# ── Eindeutigkeit im Vault ─────────────────────────────────────────────────


def test_vergebenes_symbol_faellt_auf_die_zaehlung_zurueck() -> None:
    """Ist das Symbol im Vault schon belegt, gewinnt die Eindeutigkeit.

    Ohne diesen Rueckfall liefe der Lauf in den Unique-Index
    ``(vault_id, pseudonym)`` und braeche mit einem 500 ab.
    """
    from backend.db import get_sessionmaker, init_db, reset_engine_cache
    from backend.vault import service as vault_service
    from backend.vault.models import Tenant, Vault

    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "e" * 64)
    from backend.config import get_settings

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        with get_sessionmaker()() as session:
            tenant = Tenant(name="Test")
            session.add(tenant)
            session.flush()
            vault = Vault(tenant_id=tenant.id, label="Test")
            session.add(vault)
            session.flush()

            erste = vault_service.get_or_create_pseudonym(
                session, vault, "ORGANIZATION", "Talblick",
                festes_pseudonym="[[LIEFERANT_A]]",
            )
            zweite = vault_service.get_or_create_pseudonym(
                session, vault, "ORGANIZATION", "Seeblick",
                festes_pseudonym="[[LIEFERANT_A]]",
            )

            assert erste == "[[LIEFERANT_A]]"
            assert zweite != erste
            assert zweite.startswith("[[ORGANIZATION_")
    finally:
        reset_engine_cache()
        mp.undo()
        get_settings.cache_clear()


def test_bestehende_zuordnung_wird_nicht_umgeschrieben() -> None:
    """Ein nachtraeglich gesetztes festes Pseudonym aendert alte Vaults nicht.

    Sonst zeigte ein bereits ausgeliefertes Dokument auf ein Symbol, das der
    Vault nicht mehr fuehrt — die Rueckfuehrung liefe ins Leere.
    """
    from backend.db import get_sessionmaker, init_db, reset_engine_cache
    from backend.vault import service as vault_service
    from backend.vault.models import Tenant, Vault

    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "f" * 64)
    from backend.config import get_settings

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        with get_sessionmaker()() as session:
            tenant = Tenant(name="Test")
            session.add(tenant)
            session.flush()
            vault = Vault(tenant_id=tenant.id, label="Test")
            session.add(vault)
            session.flush()

            vorher = vault_service.get_or_create_pseudonym(
                session, vault, "ORGANIZATION", "Talblick"
            )
            nachher = vault_service.get_or_create_pseudonym(
                session, vault, "ORGANIZATION", "Talblick",
                festes_pseudonym="[[LIEFERANT_A]]",
            )

            assert vorher.startswith("[[ORGANIZATION_")
            assert nachher == vorher
    finally:
        reset_engine_cache()
        mp.undo()
        get_settings.cache_clear()
