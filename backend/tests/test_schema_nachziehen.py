"""Tests des Spalten-Nachtrags (``backend.db.ensure_schema``).

Der Schritt laeuft im Lifespan der Anwendung, also bei jedem Knoten-Neustart.
Faellt er aus, trifft die neue Fassung auf ein Schema, das sie nicht kennt — und
weil die Begriffsliste bei jedem Lauf gelesen wird, waere das Werkzeug ganz
ausser Betrieb. Entsprechend wird hier nicht die Absicht geprueft, sondern das
Verhalten an einer Datenbank, der die Spalte tatsaechlich fehlt.

Der Aufbau ist bewusst grob: eine SQLite-Datei, in der ``custom_terms`` **ohne**
die neue Spalte angelegt wird — genau der Zustand einer Produktionsdatenbank vor
dem Deploy.

Warum es hier keinen Test gegen PostgreSQL gibt: Der Advisory-Lock ist der
einzige Teil, der dialektabhaengig ist, und ein PostgreSQL-Dienst gehoert nicht
in diese Testsuite. Der Lock ist deshalb bewusst so gebaut, dass er nichts
entscheidet — er serialisiert nur; die Pruefung davor und das ``ALTER`` danach
sind auf beiden Dialekten dieselben.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.db import NACHZUZIEHENDE_SPALTEN, ensure_schema  # noqa: E402
from scripts import schema_nachziehen  # noqa: E402


def _alte_tabelle(engine) -> None:
    """Legt ``custom_terms`` im Stand *vor* der Aenderung an."""
    with engine.begin() as verbindung:
        verbindung.execute(
            text(
                "CREATE TABLE custom_terms ("
                " id CHAR(32) PRIMARY KEY,"
                " tenant_id CHAR(32) NOT NULL,"
                " term VARCHAR(500) NOT NULL,"
                " entity_type VARCHAR(50) NOT NULL,"
                " match_mode VARCHAR(20) NOT NULL,"
                " created_at DATETIME NOT NULL)"
            )
        )


def test_fehlende_spalte_wird_angelegt(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alt.db'}")
    _alte_tabelle(engine)

    ergaenzt = ensure_schema(engine)

    assert ("custom_terms", "replacement") in ergaenzt
    spalten = {s["name"] for s in inspect(engine).get_columns("custom_terms")}
    assert "replacement" in spalten


def test_zweiter_lauf_aendert_nichts(tmp_path: Path) -> None:
    """Idempotenz — jeder Neustart ruft den Schritt erneut auf."""
    engine = create_engine(f"sqlite:///{tmp_path / 'neu.db'}")
    _alte_tabelle(engine)

    ensure_schema(engine)

    assert ensure_schema(engine) == []


def test_bestehende_daten_bleiben_erhalten(tmp_path: Path) -> None:
    """Rein additiv: der Nachtrag darf keine Zeile anfassen.

    Die Zuordnungstabelle ist der einzige Datenbestand, dessen Verlust nicht zu
    reparieren ist — dieser Test ist die Absicherung gegen ein ``DROP``, das
    sich irgendwann in die Liste verirrt.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'daten.db'}")
    _alte_tabelle(engine)
    with engine.begin() as verbindung:
        verbindung.execute(
            text(
                "INSERT INTO custom_terms VALUES "
                "('a'*32, 'b'*32, 'Talblick', 'ORGANIZATION', 'exact', "
                "'2026-08-19 00:00:00')"
            )
        )

    ensure_schema(engine)

    with engine.begin() as verbindung:
        zeilen = verbindung.execute(
            text("SELECT term, entity_type, replacement FROM custom_terms")
        ).all()
    assert zeilen == [("Talblick", "ORGANIZATION", None)]


def test_fehlende_tabelle_wird_uebersprungen(tmp_path: Path) -> None:
    """Auf einer leeren Datenbank ist nichts nachzuziehen — init_db legt an."""
    engine = create_engine(f"sqlite:///{tmp_path / 'leer.db'}")

    assert ensure_schema(engine) == []


def test_jede_nachzuziehende_spalte_steht_auch_im_modell() -> None:
    """Waechter gegen eine Liste, die vom Modell abdriftet.

    Ein Eintrag, den es im Modell nicht (mehr) gibt, wuerde in Produktion eine
    Spalte anlegen, die niemand liest — und der umgekehrte Fall faellt in der
    Testsuite ohnehin auf, weil ``create_all`` sie dort immer mitanlegt.
    """
    from backend.vault.models import Base

    for tabelle, spalte, _typ in NACHZUZIEHENDE_SPALTEN:
        assert tabelle in Base.metadata.tables, f"Tabelle {tabelle} gibt es nicht"
        assert spalte in Base.metadata.tables[tabelle].columns, (
            f"Spalte {tabelle}.{spalte} steht nicht im Modell"
        )


def test_nur_additive_eintraege() -> None:
    """Die Liste darf ausschliesslich Spalten hinzufuegen.

    Der SQL-Typ landet unveraendert in einem ``ALTER TABLE … ADD COLUMN``. Ein
    Eintrag, der dort etwas anderes einschleust, waere eine Codeausfuehrung auf
    dem sensibelsten Datenbestand des Systems.
    """
    verboten = ("drop", "delete", "truncate", "rename", ";", "--")
    for tabelle, spalte, sql_typ in NACHZUZIEHENDE_SPALTEN:
        for teil in (tabelle, spalte, sql_typ):
            assert not any(wort in teil.lower() for wort in verboten), (
                f"Verdaechtiger Eintrag: {teil!r}"
            )


# ── Das Skript fuer den Hand-Aufruf ────────────────────────────────────────


def test_skript_meldet_fehlende_spalte(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'skript.db'}")
    _alte_tabelle(engine)

    offen = schema_nachziehen.fehlende_spalten(engine)

    assert ("custom_terms", "replacement", "VARCHAR(100)") in offen


def test_skript_ziel_ohne_passwort(tmp_path: Path) -> None:
    """Das Protokoll soll die Datenbank zeigen, nicht die Zugangsdaten."""
    engine = create_engine("postgresql+psycopg://nutzer:geheim@db.example:5432/pti")

    beschreibung = schema_nachziehen.ziel_beschreibung(engine)

    assert "db.example" in beschreibung and "pti" in beschreibung
    assert "geheim" not in beschreibung


# ── Der Weg, den die Produktion tatsaechlich geht ──────────────────────────


def test_anwendung_repariert_altes_schema_beim_start(tmp_path: Path) -> None:
    """Der Ernstfall: Knoten-Neustart auf einer Datenbank im alten Stand.

    Das ist der Ablauf, den ein Deploy ausloest — und die Stelle, an der ein
    Fehler das Werkzeug ausser Betrieb setzte: Die Begriffsliste wird bei jedem
    Pseudonymisierungslauf gelesen, eine fehlende Spalte darin macht nicht nur
    ``/terms`` kaputt, sondern jeden Lauf.

    Geprueft wird deshalb bis zum Ende durch: Spalte da, Begriff anlegbar,
    festes Pseudonym wirkt.
    """
    import pytest
    from fastapi.testclient import TestClient

    datenbank = tmp_path / "produktion.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", f"sqlite:///{datenbank}")
    mp.setenv("VAULT_MASTER_KEY", "a" * 64)

    from backend.config import get_settings
    from backend.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    try:
        # Stand vor der Aenderung herstellen: alles anlegen, dann die neue
        # Spalte wieder entfernen.
        init_db()
        engine = create_engine(f"sqlite:///{datenbank}")
        with engine.begin() as verbindung:
            verbindung.execute(
                text("ALTER TABLE custom_terms DROP COLUMN replacement")
            )
        assert "replacement" not in {
            s["name"] for s in inspect(engine).get_columns("custom_terms")
        }
        reset_engine_cache()

        from backend.main import create_app

        with TestClient(create_app()) as client:
            assert "replacement" in {
                s["name"] for s in inspect(engine).get_columns("custom_terms")
            }

            angelegt = client.post(
                "/api/v1/terms",
                json={
                    "term": "Talblick",
                    "entity_type": "ORGANIZATION",
                    "match_mode": "normalized",
                    "replacement": "[[LIEFERANT_A]]",
                },
            )
            assert angelegt.status_code == 201, angelegt.text

            lauf = client.post(
                "/api/v1/documents:anonymize",
                json={
                    "text": "Vertrag mit Talblick.",
                    "mode": "reversible",
                    "entities": ["ORGANIZATION"],
                },
            )
            assert lauf.status_code == 200, lauf.text
            assert lauf.json()["text"] == "Vertrag mit [[LIEFERANT_A]]."
    finally:
        reset_engine_cache()
        mp.undo()
        get_settings.cache_clear()


def test_neue_auftragsspalten_stehen_in_der_nachzugsliste() -> None:
    """Ohne Eintrag entsteht die Spalte in Produktion nicht.

    `create_all()` legt fehlende TABELLEN an, aber keine fehlenden SPALTEN.
    Der Fehler faellt dann nicht beim Deploy auf, sondern beim ersten Zugriff —
    und weil die Auftragstabelle bei jedem grossen Upload gelesen wird, waere
    das Werkzeug damit ausser Betrieb.
    """
    from backend.db import NACHZUZIEHENDE_SPALTEN
    from backend.vault.models import Job

    eingetragen = {spalte for tabelle, spalte, _ in NACHZUZIEHENDE_SPALTEN if tabelle == "jobs"}
    neu = {
        "eingabe_blob", "dateiname", "parameter_json", "ergebnis_blob",
        "fortschritt", "gesamt", "fehler", "aktor", "updated_at", "freigaben",
    }
    assert neu <= eingetragen, f"nicht nachgezogen: {sorted(neu - eingetragen)}"
    assert neu <= set(Job.__table__.columns.keys())
