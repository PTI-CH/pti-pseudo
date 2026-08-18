"""Tests des Begriffslisten-Imports (``scripts/begriffe_importieren.py``).

Die Tests laufen wie die übrigen Vault-Tests gegen **SQLite in-memory** —
Begründung und die nötigen Portabilitäts-Entscheidungen stehen im Kopf von
``backend/vault/models.py``. Geprüft werden die Bausteine, die auch die
Kommandozeile benutzt (:func:`sammle_kandidaten`, :func:`validiere_alle`,
:func:`importiere`), nicht ``main`` selbst — so bleiben die Tests unabhängig
davon, welche Datenbank-URL gerade in der Umgebung steht.

Alle Testbegriffe sind frei erfunden (Hausregel: keine echten Personen- oder
Organisationsdaten im Repository) und haben nichts mit der echten
671-Begriffe-Importdatei zu tun, die ausdrücklich nicht ins Repo gehört.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.db import create_db_engine, init_db  # noqa: E402
from backend.vault.models import CustomTerm, Tenant  # noqa: E402
from scripts import begriffe_importieren as importer  # noqa: E402


@pytest.fixture
def session() -> Iterator[Session]:
    """Frische SQLite-In-Memory-Datenbank je Test.

    ``create_db_engine`` erzwingt für SQLite den ``StaticPool`` — ohne ihn
    sähe jede Verbindung eine eigene, leere Datenbank (siehe
    ``backend/db.py``).
    """
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as offene_session:
        yield offene_session
    engine.dispose()


@pytest.fixture
def tenant(session: Session) -> Tenant:
    """Ein Mandant, wie ihn ``get_tenant`` liefern würde."""
    neuer_tenant = Tenant(name="PTI")
    session.add(neuer_tenant)
    session.flush()
    return neuer_tenant


def bestand(session: Session, tenant: Tenant) -> list[CustomTerm]:
    """Alle Begriffsliste-Einträge des Mandanten, sortiert nach Begriff."""
    return list(
        session.execute(
            select(CustomTerm)
            .where(CustomTerm.tenant_id == tenant.id)
            .order_by(CustomTerm.term)
        ).scalars()
    )


#: Eine kleine, in sich konsistente Beispiel-Eingabedatei für die Tests.
BEISPIELDATEN = {
    "sicher": [
        {
            "term": "Kantonspolizei Musterhausen",
            "entity_type": "ORGANIZATION",
            "match_mode": "normalized",
            "zu_pruefen": False,
            "quellen": ["crm-ppm"],
        },
        {
            "term": "Projekt Nachtfalter",
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "zu_pruefen": False,
            "quellen": ["translator-glossar"],
        },
    ],
    "zu_pruefen": [
        {
            "term": "MEF",
            "entity_type": "ORGANIZATION",
            "match_mode": "exact",
            "zu_pruefen": True,
            "quellen": ["crm-ppm"],
        },
    ],
}


# ── Testfall 1: Import legt die erwartete Anzahl an ────────────────────────


def test_import_legt_erwartete_anzahl_mit_korrektem_typ_und_modus_an(
    session: Session, tenant: Tenant
) -> None:
    """Zwei ``sicher``-Einträge werden mit ihrem Typ und Modus angelegt."""
    kandidaten = importer.sammle_kandidaten(BEISPIELDATEN, auch_zu_pruefen=False)
    begriffe, ungueltig, warnungen = importer.validiere_alle(kandidaten)
    assert ungueltig == []
    assert warnungen == set()

    zusammenfassung = importer.importiere(session, tenant, begriffe, dry_run=False)

    assert zusammenfassung.anzahl_angelegt == 2
    assert zusammenfassung.angelegt == {"ORGANIZATION": 2}

    eintraege = {e.term: e for e in bestand(session, tenant)}
    assert set(eintraege) == {"Kantonspolizei Musterhausen", "Projekt Nachtfalter"}
    assert eintraege["Kantonspolizei Musterhausen"].entity_type == "ORGANIZATION"
    assert eintraege["Kantonspolizei Musterhausen"].match_mode == "normalized"
    assert eintraege["Projekt Nachtfalter"].match_mode == "exact"


# ── Testfall 2: Idempotenz ─────────────────────────────────────────────────


def test_zweiter_lauf_legt_nichts_neues_an(session: Session, tenant: Tenant) -> None:
    """Ein zweiter Lauf über dieselbe Datei erzeugt keine Dubletten."""
    kandidaten = importer.sammle_kandidaten(BEISPIELDATEN, auch_zu_pruefen=False)
    begriffe, _, _ = importer.validiere_alle(kandidaten)

    erster_lauf = importer.importiere(session, tenant, begriffe, dry_run=False)
    assert erster_lauf.anzahl_angelegt == 2

    zweiter_lauf = importer.importiere(session, tenant, begriffe, dry_run=False)

    assert zweiter_lauf.anzahl_angelegt == 0
    assert zweiter_lauf.anzahl_bestehend == 2
    assert len(bestand(session, tenant)) == 2


def test_dublette_innerhalb_derselben_datei_wird_nur_einmal_angelegt(
    session: Session, tenant: Tenant
) -> None:
    """Derselbe Begriff zweimal in der Datei -> nur der erste Treffer zählt.

    Bildet den echten Fall aus der 671er-Datei nach, wo z. B. "POLAP" sowohl
    als PTI_PRODUKT als auch als PTI_PROJEKT vorkommt — die Gleichheit läuft
    über ``term`` allein, nicht über ``(term, entity_type)``.
    """
    daten = {
        "sicher": [
            {
                "term": "POLAP",
                "entity_type": "ORGANIZATION",
                "match_mode": "exact",
                "zu_pruefen": False,
                "quellen": ["crm-ppm"],
            },
            {
                "term": "POLAP",
                "entity_type": "PTI_PRODUKT",
                "match_mode": "exact",
                "zu_pruefen": False,
                "quellen": ["crm-ppm"],
            },
        ],
        "zu_pruefen": [],
    }
    kandidaten = importer.sammle_kandidaten(daten, auch_zu_pruefen=False)
    begriffe, ungueltig, _ = importer.validiere_alle(kandidaten)
    assert ungueltig == []

    zusammenfassung = importer.importiere(session, tenant, begriffe, dry_run=False)

    assert zusammenfassung.anzahl_angelegt == 1
    assert zusammenfassung.datei_dubletten == 1
    eintraege = bestand(session, tenant)
    assert len(eintraege) == 1
    assert eintraege[0].entity_type == "ORGANIZATION"  # der erste Treffer gewinnt


# ── Testfall 3: --dry-run schreibt nichts ──────────────────────────────────


def test_dry_run_schreibt_nichts(session: Session, tenant: Tenant) -> None:
    """Ein Probelauf zählt wie ein echter Lauf, verändert die Datenbank aber nicht."""
    kandidaten = importer.sammle_kandidaten(BEISPIELDATEN, auch_zu_pruefen=False)
    begriffe, _, _ = importer.validiere_alle(kandidaten)

    zusammenfassung = importer.importiere(session, tenant, begriffe, dry_run=True)

    assert zusammenfassung.anzahl_angelegt == 2
    assert bestand(session, tenant) == []


# ── Testfall 4: zu_pruefen nur mit --auch-zu-pruefen ───────────────────────


def test_zu_pruefen_bleibt_ohne_option_aussen_vor(
    session: Session, tenant: Tenant
) -> None:
    """Ohne ``--auch-zu-pruefen`` fehlt der zu_pruefen-Begriff im Import."""
    kandidaten = importer.sammle_kandidaten(BEISPIELDATEN, auch_zu_pruefen=False)
    begriffe, _, _ = importer.validiere_alle(kandidaten)

    assert {b.term for b in begriffe} == {
        "Kantonspolizei Musterhausen",
        "Projekt Nachtfalter",
    }

    zusammenfassung = importer.importiere(session, tenant, begriffe, dry_run=False)
    assert zusammenfassung.anzahl_angelegt == 2
    assert "MEF" not in {e.term for e in bestand(session, tenant)}


def test_zu_pruefen_kommt_mit_option_mit(session: Session, tenant: Tenant) -> None:
    """Mit ``--auch-zu-pruefen`` wird der zu_pruefen-Begriff mitimportiert."""
    kandidaten = importer.sammle_kandidaten(BEISPIELDATEN, auch_zu_pruefen=True)
    begriffe, _, _ = importer.validiere_alle(kandidaten)

    assert {b.term for b in begriffe} == {
        "Kantonspolizei Musterhausen",
        "Projekt Nachtfalter",
        "MEF",
    }

    zusammenfassung = importer.importiere(session, tenant, begriffe, dry_run=False)
    assert zusammenfassung.anzahl_angelegt == 3
    assert "MEF" in {e.term for e in bestand(session, tenant)}


# ── Testfall 5: ungültige Einträge werden abgelehnt ────────────────────────


def test_ungueltiger_entity_type_wird_abgelehnt(
    session: Session, tenant: Tenant
) -> None:
    """Ein unbekannter entity_type landet nicht in der Datenbank."""
    daten = {
        "sicher": [
            {
                "term": "Gueltige Organisation",
                "entity_type": "ORGANIZATION",
                "match_mode": "exact",
                "zu_pruefen": False,
                "quellen": [],
            },
            {
                "term": "Nicht Katalogisiert",
                "entity_type": "VOELLIG_UNBEKANNT",
                "match_mode": "exact",
                "zu_pruefen": False,
                "quellen": [],
            },
        ],
        "zu_pruefen": [],
    }
    kandidaten = importer.sammle_kandidaten(daten, auch_zu_pruefen=False)
    begriffe, ungueltig, _ = importer.validiere_alle(kandidaten)

    assert {b.term for b in begriffe} == {"Gueltige Organisation"}
    assert len(ungueltig) == 1
    assert "VOELLIG_UNBEKANNT" in ungueltig[0][1]

    zusammenfassung = importer.importiere(session, tenant, begriffe, dry_run=False)
    assert zusammenfassung.anzahl_angelegt == 1
    eintraege = {e.term for e in bestand(session, tenant)}
    assert "Nicht Katalogisiert" not in eintraege


def test_ungueltiger_match_mode_wird_abgelehnt(
    session: Session, tenant: Tenant
) -> None:
    """Ein nicht unterstützter match_mode landet nicht in der Datenbank."""
    daten = {
        "sicher": [
            {
                "term": "Falscher Modus AG",
                "entity_type": "ORGANIZATION",
                "match_mode": "fuzzy",  # kein gültiger MATCH_MODES-Wert
                "zu_pruefen": False,
                "quellen": [],
            },
        ],
        "zu_pruefen": [],
    }
    kandidaten = importer.sammle_kandidaten(daten, auch_zu_pruefen=False)
    begriffe, ungueltig, _ = importer.validiere_alle(kandidaten)

    assert begriffe == []
    assert len(ungueltig) == 1
    assert "fuzzy" in ungueltig[0][1]


def test_zu_kurzer_begriff_wird_abgelehnt(session: Session, tenant: Tenant) -> None:
    """Ein Begriff mit weniger als drei Zeichen nach dem Trimmen wird abgelehnt."""
    daten = {
        "sicher": [
            {
                "term": "  ab  ",
                "entity_type": "ORGANIZATION",
                "match_mode": "exact",
                "zu_pruefen": False,
                "quellen": [],
            },
        ],
        "zu_pruefen": [],
    }
    kandidaten = importer.sammle_kandidaten(daten, auch_zu_pruefen=False)
    begriffe, ungueltig, _ = importer.validiere_alle(kandidaten)

    assert begriffe == []
    assert len(ungueltig) == 1


# ── Zusatz: Katalog-Warnung für PTI_PROJEKT/PTI_PRODUKT ────────────────────


def test_fehlender_katalogtyp_wird_nur_verwarnt_nicht_abgelehnt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fehlt PTI_PRODUKT (noch) im Katalog, wird der Eintrag trotzdem übernommen.

    Bildet die Übergangsphase nach, in der ein paralleler Arbeitsstrang die
    Typen PTI_PROJEKT/PTI_PRODUKT erst noch in ``backend/engine/entities.py``
    einträgt — unabhängig davon, ob sie im Katalog dieses Testlaufs bereits
    stehen.
    """
    monkeypatch.setattr(
        importer.ents,
        "ALL_ENTITIES",
        [t for t in importer.ents.ALL_ENTITIES if t != "PTI_PRODUKT"],
    )

    warnungen: set[str] = set()
    begriff, grund = importer.pruefe_eintrag(
        {
            "term": "MEF",
            "entity_type": "PTI_PRODUKT",
            "match_mode": "exact",
        },
        "sicher",
        warnungen,
    )

    assert grund is None
    assert begriff is not None
    assert begriff.entity_type == "PTI_PRODUKT"
    assert warnungen == {"PTI_PRODUKT"}


def test_datei_ohne_erwartete_listen_bricht_kontrolliert_ab(
    tmp_path: Path,
) -> None:
    """Eine Eingabedatei ohne die Liste 'sicher' führt zu einem klaren Abbruch."""
    kaputte_datei = tmp_path / "kaputt.json"
    kaputte_datei.write_text('{"zu_pruefen": []}', encoding="utf-8")

    with pytest.raises(SystemExit):
        importer.lade_datei(kaputte_datei)
