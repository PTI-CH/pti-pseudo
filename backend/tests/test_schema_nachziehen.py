"""Tests des Schema-Nachzugs (``scripts/schema_nachziehen.py``).

Dieses Skript laeuft im Deploy zwischen Upload und Neustart. Faellt es aus,
trifft die neue Fassung auf ein Schema, das sie nicht kennt — und weil die
Begriffsliste bei jedem Lauf gelesen wird, waere das Werkzeug ganz ausser
Betrieb. Entsprechend wird hier nicht die Absicht geprueft, sondern das
Verhalten an einer Datenbank, der die Spalte tatsaechlich fehlt.

Der Aufbau ist bewusst grob: eine SQLite-Datei, in der die Tabelle **ohne** die
neue Spalte angelegt wird — genau der Zustand einer Produktionsdatenbank vor
dem Deploy.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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


def test_fehlende_spalte_wird_erkannt_und_angelegt(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alt.db'}")
    _alte_tabelle(engine)

    offen = schema_nachziehen.fehlende_spalten(engine)
    assert ("custom_terms", "replacement", "VARCHAR(100)") in offen

    for tabelle, spalte, sql_typ in offen:
        with engine.begin() as verbindung:
            verbindung.execute(
                text(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {sql_typ}")
            )

    assert schema_nachziehen.fehlende_spalten(engine) == []
    spalten = {s["name"] for s in inspect(engine).get_columns("custom_terms")}
    assert "replacement" in spalten


def test_zweiter_lauf_findet_nichts_mehr(tmp_path: Path) -> None:
    """Idempotenz — ein Deploy ohne Schemaaenderung muss durchlaufen."""
    engine = create_engine(f"sqlite:///{tmp_path / 'neu.db'}")
    _alte_tabelle(engine)
    with engine.begin() as verbindung:
        verbindung.execute(
            text("ALTER TABLE custom_terms ADD COLUMN replacement VARCHAR(100)")
        )

    assert schema_nachziehen.fehlende_spalten(engine) == []


def test_fehlende_tabelle_wird_uebersprungen(tmp_path: Path) -> None:
    """Auf einer leeren Datenbank ist nichts nachzuziehen — init_db legt an."""
    engine = create_engine(f"sqlite:///{tmp_path / 'leer.db'}")

    assert schema_nachziehen.fehlende_spalten(engine) == []


def test_jede_nachzuziehende_spalte_steht_auch_im_modell() -> None:
    """Waechter gegen eine Liste, die vom Modell abdriftet.

    Ein Eintrag, den es im Modell nicht (mehr) gibt, wuerde in Produktion eine
    Spalte anlegen, die niemand liest — und der umgekehrte Fall faellt in der
    Testsuite ohnehin auf, weil ``create_all`` sie dort immer mitanlegt.
    """
    from backend.vault.models import Base

    for tabelle, spalte, _typ in schema_nachziehen.NACHZUZIEHEN:
        assert tabelle in Base.metadata.tables, f"Tabelle {tabelle} gibt es nicht"
        assert spalte in Base.metadata.tables[tabelle].columns, (
            f"Spalte {tabelle}.{spalte} steht nicht im Modell"
        )


def test_ziel_wird_ohne_passwort_benannt(tmp_path: Path) -> None:
    """Das Deploy-Protokoll soll die Datenbank zeigen, nicht die Zugangsdaten."""
    engine = create_engine("postgresql+psycopg://nutzer:geheim@db.example:5432/pti")

    beschreibung = schema_nachziehen.ziel_beschreibung(engine)

    assert "db.example" in beschreibung and "pti" in beschreibung
    assert "geheim" not in beschreibung
