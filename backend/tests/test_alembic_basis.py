"""Waechter der Basismigration: sie muss beschreiben, was die Modelle sagen.

Die Zusicherung
---------------
``migrations/versions/2026-08-26_basis.py`` ist der **Ist-Zustand** der
Produktionsdatenbank. Driftet sie von ``backend.vault.models`` ab — weil jemand
eine Spalte ins Modell schreibt und die Migration vergisst —, dann bekommt eine
neu aufgebaute Datenbank ein anderes Schema als die laufende, und der
Unterschied faellt erst beim ersten Zugriff auf.

Warum der Vergleich auf **PostgreSQL-DDL** laeuft und nicht auf SQLite
-----------------------------------------------------------------------
Die Testsuite laeuft auf SQLite, Produktion auf PostgreSQL — und genau die
beiden Typen, bei denen ``--autogenerate`` erfahrungsgemaess irrt, sehen auf
SQLite gleich aus und auf PostgreSQL verschieden:

* ``JSON().with_variant(JSONB(), "postgresql")`` rendert auf SQLite in **beiden**
  Faellen ``JSON``. Eine Migration, die den Variantenzusatz verloren hat, waere
  auf SQLite nicht von der richtigen zu unterscheiden — in Produktion stuende
  dann ``JSON`` statt ``JSONB``.
* ``sa.Uuid`` rendert auf SQLite beidseitig ``CHAR(32)``, auf PostgreSQL
  ``UUID``.

Ein Vergleich auf SQLite waere fuer die Fehler, die tatsaechlich schaden, also
blind. Deshalb wird die DDL **ohne Datenbank** in beiden Richtungen fuer den
PostgreSQL-Dialekt erzeugt:

* aus der Migration ueber Alembics Offline-Modus (``upgrade --sql``; er
  verbindet sich nicht, er braucht von der URL nur den Dialekt),
* aus den Modellen ueber ``create_mock_engine`` (dasselbe: kein Verbindungsaufbau).

Der zweite Test laesst die Migration zusaetzlich **wirklich laufen**, gegen eine
leere SQLite-Datei. Der DDL-Vergleich oben pruefte sonst nur, was die Migration
*erzeugen wuerde* — nicht, ob sie ueberhaupt durchlaeuft.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, create_mock_engine, inspect, text

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.vault.models import Base  # noqa: E402

#: Dialekt-URL ohne echten Host. Weder der Offline-Modus noch die Mock-Engine
#: bauen eine Verbindung auf — von der URL wird ausschliesslich der Dialekt
#: gelesen. Es gibt in dieser Testsuite keine PostgreSQL, und es soll auch
#: keine gebraucht werden.
_PG_URL = "postgresql+psycopg://niemand:nichts@nirgends:5432/keine"

#: Alembics eigene Buchfuehrungstabelle. Sie steht in keinem Modell und darf
#: den Vergleich nicht stoeren.
_ALEMBIC_TABELLE = "alembic_version"


def _alembic_config(**optionen) -> Config:
    """Baut eine Alembic-Konfiguration **ohne** ``alembic.ini``.

    Bewusst ohne Datei: ``env.py`` ruft ``fileConfig`` nur, wenn Alembic mit
    einer ini gestartet wurde. Im Test soll das Protokoll der Testsuite
    unangetastet bleiben — ``test_protokoll.py`` prueft daneben genau daran
    herum.

    Parameters
    ----------
    **optionen
        Weitere Schluessel im Abschnitt ``[alembic]``.

    Returns
    -------
    Config
        Die fertige Konfiguration mit gesetztem ``script_location``.
    """
    config = Config(**optionen)
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return config


def _normalisiere(anweisungen: list[str]) -> set[str]:
    """Macht DDL-Anweisungen vergleichbar.

    Alembic und ``create_all`` erzeugen dieselben Anweisungen, aber mit
    unterschiedlicher Einrueckung (Tabs gegen Leerzeichen) und in
    unterschiedlicher Reihenfolge. Verglichen wird deshalb eine **Menge**
    weissraumnormalisierter Zeichenketten.

    Parameters
    ----------
    anweisungen
        Rohe DDL-Anweisungen.

    Returns
    -------
    set of str
        Je Anweisung eine Zeile ohne Mehrfach-Weissraum und ohne Semikolon.
    """
    gesaeubert = set()
    for anweisung in anweisungen:
        eine_zeile = re.sub(r"\s+", " ", anweisung).strip().rstrip(";").strip()
        if eine_zeile:
            gesaeubert.add(eine_zeile)
    return gesaeubert


def _ddl_aus_der_migration() -> set[str]:
    """PostgreSQL-DDL, die die Basismigration erzeugen wuerde — ohne Datenbank."""
    puffer = io.StringIO()
    config = _alembic_config(output_buffer=puffer)
    config.set_main_option("sqlalchemy.url", _PG_URL)

    command.upgrade(config, "head", sql=True)

    anweisungen = []
    for teil in puffer.getvalue().split(";"):
        # Alembics Kommentarzeilen ("-- Running upgrade …") und BEGIN/COMMIT
        # gehoeren nicht zum Schema.
        ohne_kommentar = "\n".join(
            zeile for zeile in teil.splitlines() if not zeile.strip().startswith("--")
        )
        if ohne_kommentar.strip().upper() in {"BEGIN", "COMMIT", ""}:
            continue
        anweisungen.append(ohne_kommentar)
    return _normalisiere(anweisungen)


def _ddl_aus_den_modellen() -> set[str]:
    """PostgreSQL-DDL, die ``create_all`` erzeugen wuerde — ohne Datenbank."""
    anweisungen: list[str] = []

    def sammle(sql, *args, **kwargs) -> None:
        anweisungen.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine(_PG_URL, sammle)
    Base.metadata.create_all(engine, checkfirst=False)
    return _normalisiere(anweisungen)


def _nur_schema(anweisungen: set[str]) -> set[str]:
    """Streift Alembics eigene Buchfuehrung ab."""
    return {a for a in anweisungen if _ALEMBIC_TABELLE not in a}


def test_basismigration_beschreibt_genau_das_modellschema() -> None:
    """Der eigentliche Waechter: gleiche PostgreSQL-DDL aus beiden Quellen.

    Reisst dieser Test, ist meistens eine Migration vergessen worden: Eine
    Aenderung an ``backend/vault/models.py`` ohne
    ``alembic revision --autogenerate`` daneben.
    """
    aus_migration = _nur_schema(_ddl_aus_der_migration())
    aus_modellen = _nur_schema(_ddl_aus_den_modellen())

    fehlt_in_der_migration = aus_modellen - aus_migration
    zu_viel_in_der_migration = aus_migration - aus_modellen

    assert not fehlt_in_der_migration, (
        "Die Migrationen erzeugen nicht, was die Modelle beschreiben. "
        f"Fehlt: {sorted(fehlt_in_der_migration)}"
    )
    assert not zu_viel_in_der_migration, (
        "Die Migrationen erzeugen mehr, als die Modelle beschreiben. "
        f"Zu viel: {sorted(zu_viel_in_der_migration)}"
    )


def test_jsonb_bleibt_jsonb() -> None:
    """Die Stelle, an der ``--autogenerate`` am ehesten irrt.

    Die JSON-Spalten sind ``JSON().with_variant(JSONB(), "postgresql")``. Geht
    der Variantenzusatz in einer Migration verloren, ist das auf SQLite
    unsichtbar und in Produktion eine andere Spalte. Der Test oben faenge es
    mit ab — diese ausdrueckliche Fassung sagt beim Reissen, *was* kaputt ist.
    """
    ddl = " ".join(sorted(_ddl_aus_der_migration()))

    assert "quality_json JSONB" in ddl
    assert "parameter_json JSONB" in ddl
    assert "entity_counts JSONB" in ddl
    # Und die UUID-Primaerschluessel sind nicht zu CHAR(32) geworden.
    assert "id UUID NOT NULL" in ddl


def test_genau_eine_spitze() -> None:
    """Ein einziger Kopf — sonst weiss ``upgrade head`` nicht, wohin.

    Zwei parallele Revisionen entstehen leicht: zwei Branches, in jedem eine
    ``revision``, beide mit demselben ``down_revision``. Nach dem Merge
    scheitert jeder ``upgrade head`` mit „Multiple head revisions" — und zwar
    zuerst in Produktion, wenn niemand es vorher bemerkt.
    """
    from alembic.script import ScriptDirectory

    spitzen = ScriptDirectory.from_config(_alembic_config()).get_heads()

    assert list(spitzen) == ["20260826_basis"], f"unerwartete Koepfe: {spitzen}"


def test_upgrade_laeuft_wirklich_durch(tmp_path: Path) -> None:
    """Die Migration wird ausgefuehrt, nicht nur uebersetzt.

    Gegen eine leere SQLite-Datei — der Fall, den ein frisches
    Entwicklungssystem und die Ein-Knoten-Rueckfallvariante tatsaechlich gehen.
    Verglichen wird anschliessend mit einer ``create_all``-Datenbank: gleiche
    Tabellen, gleiche Spalten, gleiche Indizes.
    """
    ziel = tmp_path / "migriert.db"
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{ziel}")

    command.upgrade(config, "head")

    referenz = tmp_path / "create_all.db"
    referenz_engine = create_engine(f"sqlite:///{referenz}")
    Base.metadata.create_all(referenz_engine)

    migriert = inspect(create_engine(f"sqlite:///{ziel}"))
    erwartet = inspect(referenz_engine)

    tabellen = set(migriert.get_table_names()) - {_ALEMBIC_TABELLE}
    assert tabellen == set(erwartet.get_table_names())

    for tabelle in sorted(tabellen):
        spalten_ist = {
            (s["name"], str(s["type"]), s["nullable"])
            for s in migriert.get_columns(tabelle)
        }
        spalten_soll = {
            (s["name"], str(s["type"]), s["nullable"])
            for s in erwartet.get_columns(tabelle)
        }
        assert spalten_ist == spalten_soll, f"Spalten weichen ab in {tabelle}"

        indizes_ist = {
            (i["name"], tuple(i["column_names"]), bool(i["unique"]))
            for i in migriert.get_indexes(tabelle)
        }
        indizes_soll = {
            (i["name"], tuple(i["column_names"]), bool(i["unique"]))
            for i in erwartet.get_indexes(tabelle)
        }
        assert indizes_ist == indizes_soll, f"Indizes weichen ab in {tabelle}"


def test_die_gestempelte_revision_steht_in_der_datenbank(tmp_path: Path) -> None:
    """Nach ``stamp`` traegt die Datenbank die Revision — ohne dass etwas entsteht.

    Das ist der Weg, den die Produktionsdatenbank geht, hier an einer
    ``create_all``-Datenbank nachgestellt: Tabellen sind da, die Migration lief
    nie, gestempelt wird trotzdem.
    """
    ziel = tmp_path / "bestand.db"
    engine = create_engine(f"sqlite:///{ziel}")
    Base.metadata.create_all(engine)
    vorher = set(inspect(engine).get_table_names())

    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{ziel}")
    command.stamp(config, "head")

    with engine.connect() as verbindung:
        stand = verbindung.execute(
            text(f"SELECT version_num FROM {_ALEMBIC_TABELLE}")  # noqa: S608
        ).scalar_one()

    assert stand == "20260826_basis"
    # Gestempelt heisst: nichts angelegt ausser der Buchfuehrungstabelle.
    assert set(inspect(engine).get_table_names()) - vorher == {_ALEMBIC_TABELLE}


def test_upgrade_verweigert_sich_auf_bestehendem_bestand(tmp_path: Path) -> None:
    """Ein ``upgrade`` auf der Produktionsdatenbank waere der falsche Befehl.

    Es koennte dort nichts vernichten (PostgreSQLs DDL ist transaktional, das
    erste ``CREATE TABLE`` scheiterte an der vorhandenen Tabelle) — aber die
    Meldung waere ``DuplicateTable`` und niemand wuesste, was stattdessen zu
    tun ist. Die Sicherung in der Migration nennt den richtigen Befehl.
    """
    ziel = tmp_path / "bestand.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{ziel}"))

    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{ziel}")

    with pytest.raises(RuntimeError, match="alembic_stempeln"):
        command.upgrade(config, "head")


def test_downgrade_loescht_nichts(tmp_path: Path) -> None:
    """Kein Rueckbau der Basis — er loeschte die Zuordnungstabelle.

    Die autogenerierte Fassung enthielt genau dieses ``DROP TABLE
    mapping_entries``. Der Test ist die Sicherung dagegen, dass es
    zurueckkommt.
    """
    ziel = tmp_path / "migriert.db"
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{ziel}")
    command.upgrade(config, "head")

    with pytest.raises(RuntimeError, match="mapping_entries"):
        command.downgrade(config, "base")

    tabellen = set(inspect(create_engine(f"sqlite:///{ziel}")).get_table_names())
    assert "mapping_entries" in tabellen


def test_keine_migration_enthaelt_drop_oder_rename() -> None:
    """Rein additiv, so lange es nur die Basis gibt.

    Ein ``DROP``/``RENAME`` ist nicht fuer alle Zeit verboten — aber es gehoert
    in eine eigene, bewusst gepruefte Aenderung und nicht versehentlich in eine
    Migration, die etwas anderes tun sollte. Reisst dieser Test, ist die Frage
    zu beantworten: ist der Verlust gewollt und ist er gesichert?
    """
    verzeichnis = _ROOT / "migrations" / "versions"
    dateien = sorted(verzeichnis.glob("*.py"))
    assert dateien, "keine Migrationen gefunden — falscher Pfad?"

    for datei in dateien:
        quelltext = datei.read_text(encoding="utf-8")
        # Nur der Code, nicht Docstrings und Kommentare, in denen die
        # Begruendung fuer das Fehlen genau dieser Aufrufe steht.
        code = "\n".join(
            zeile
            for zeile in quelltext.splitlines()
            if not zeile.lstrip().startswith("#")
        )
        for verboten in ("op.drop_table(", "op.drop_column(", "op.alter_column("):
            assert verboten not in code, f"{datei.name} enthaelt {verboten}"


# ── Das Stempel-Werkzeug fuer die bestehende Datenbank ─────────────────────
#
# Der Weg, den die Produktionsdatenbank genau einmal geht. Er laesst sich hier
# vollstaendig nachstellen, weil an ihm nichts PostgreSQL-spezifisch ist:
# gelesen wird der Tabellen- und Spaltenbestand, geschrieben ausschliesslich
# ``alembic_version``.


@pytest.fixture()
def _bestand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Eine Datenbank im Stand von heute: Tabellen da, nie migriert.

    Liefert die Engine. ``DATABASE_URL`` zeigt darauf, damit das Skript sie
    ueber ``backend.db.get_engine`` findet — denselben Weg wie in Produktion.
    """
    from backend.config import get_settings
    from backend.db import reset_engine_cache

    ziel = tmp_path / "bestand.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{ziel}")
    get_settings.cache_clear()
    reset_engine_cache()

    engine = create_engine(f"sqlite:///{ziel}")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        reset_engine_cache()
        get_settings.cache_clear()


def _stand(engine) -> str | None:
    """Liest ``alembic_version`` — oder ``None``, wenn es die Tabelle nicht gibt."""
    if not inspect(engine).has_table(_ALEMBIC_TABELLE):
        return None
    with engine.connect() as verbindung:
        return verbindung.execute(
            text(f"SELECT version_num FROM {_ALEMBIC_TABELLE}")  # noqa: S608
        ).scalar_one_or_none()


def test_stempeln_weigert_sich_auf_leerer_datenbank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eine leere Datenbank zu stempeln waere eine Luege ueber ihren Zustand.

    Danach glaubte Alembic, die Basisrevision sei angewandt — und die Tabellen
    entstuenden nie, weil ``upgrade`` sie fuer erledigt haelt.
    """
    from backend.config import get_settings
    from backend.db import reset_engine_cache
    from scripts import alembic_stempeln

    ziel = tmp_path / "leer.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{ziel}")
    get_settings.cache_clear()
    reset_engine_cache()
    try:
        assert alembic_stempeln.main([]) == 1
        engine = create_engine(f"sqlite:///{ziel}")
        assert _stand(engine) is None
    finally:
        reset_engine_cache()
        get_settings.cache_clear()


def test_pruefen_schreibt_nichts(_bestand) -> None:
    """``--pruefen`` meldet nur — der erste Schritt in Produktion."""
    from scripts import alembic_stempeln

    assert alembic_stempeln.main(["--pruefen"]) == 1
    assert _stand(_bestand) is None


def test_stempeln_setzt_die_revision_und_laesst_daten_in_ruhe(_bestand) -> None:
    """Der Ernstfall: Bestand bleibt Bestand, nur die Buchfuehrung kommt dazu.

    Die Zeile in ``tenants`` steht stellvertretend fuer die
    Zuordnungstabelle — der einzige Datenbestand dieses Systems, dessen
    Verlust nicht zu reparieren ist.
    """
    import uuid

    from scripts import alembic_stempeln

    kennung = uuid.uuid4()
    with _bestand.begin() as verbindung:
        verbindung.execute(
            text("INSERT INTO tenants (id, name, created_at) VALUES (:i, :n, :c)"),
            {"i": kennung.hex, "n": "PTI", "c": "2026-08-26 00:00:00"},
        )
    vorher = set(inspect(_bestand).get_table_names())

    assert alembic_stempeln.main([]) == 0

    assert _stand(_bestand) == "20260826_basis"
    assert set(inspect(_bestand).get_table_names()) - vorher == {_ALEMBIC_TABELLE}
    with _bestand.connect() as verbindung:
        assert verbindung.execute(text("SELECT name FROM tenants")).scalars().all() == [
            "PTI"
        ]


def test_zweiter_lauf_ist_folgenlos(_bestand) -> None:
    """Idempotent: ein zweiter Aufruf meldet Vollzug und schreibt nichts."""
    from scripts import alembic_stempeln

    assert alembic_stempeln.main([]) == 0
    assert alembic_stempeln.main([]) == 0
    assert _stand(_bestand) == "20260826_basis"


def test_fremde_revision_wird_nicht_ueberschrieben(_bestand) -> None:
    """Steht dort etwas anderes, ist das von Hand zu klaeren.

    Ein stilles Ueberschreiben verloere die Information, dass zwei Staende
    auseinandergelaufen sind — und genau die braucht man dann.
    """
    from scripts import alembic_stempeln

    with _bestand.begin() as verbindung:
        verbindung.execute(
            text(f"CREATE TABLE {_ALEMBIC_TABELLE} (version_num VARCHAR(32) NOT NULL)")
        )
        verbindung.execute(
            text(f"INSERT INTO {_ALEMBIC_TABELLE} VALUES ('irgendwas_anderes')")  # noqa: S608
        )

    assert alembic_stempeln.main([]) == 1
    assert _stand(_bestand) == "irgendwas_anderes"


def test_fehlende_spalte_verhindert_das_stempeln(_bestand) -> None:
    """Ein halbes Schema wird nicht als vollstaendig gestempelt.

    Der Fall ist real: eine Datenbank, die seit dem letzten Deploy nicht mehr
    gestartet wurde, hat die Spalten aus ``NACHZUZIEHENDE_SPALTEN`` noch
    nicht. Erst die Anwendung starten lassen, dann stempeln.
    """
    from scripts import alembic_stempeln

    with _bestand.begin() as verbindung:
        verbindung.execute(text("ALTER TABLE custom_terms DROP COLUMN replacement"))

    assert alembic_stempeln.main([]) == 1
    assert _stand(_bestand) is None
