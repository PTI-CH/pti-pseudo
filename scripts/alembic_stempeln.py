r"""Stempelt eine **bestehende** Datenbank auf die Alembic-Basisrevision.

Warum es dieses Skript gibt und nicht bloss ``alembic stamp head``
------------------------------------------------------------------
``stamp`` schreibt eine Zeile in ``alembic_version`` und prueft dabei **nichts**.
Auf der falschen Datenbank ausgefuehrt, behauptet es hinterher, ein Schema sei
auf einem Stand, den es nie hatte — und die naechste echte Migration liefe auf
einer Datenbank, deren Ausgangszustand ein anderer ist als angenommen. Auf der
Zuordnungstabelle des Vaults ist das kein akademisches Risiko.

Dieses Skript prueft deshalb vorher und meldet, gegen **welche** Datenbank es
gelaufen ist:

* Sind die Tabellen der Basisrevision ueberhaupt da? Eine leere Datenbank wird
  nicht gestempelt, sondern gehoert entweder der Anwendung ueberlassen
  (``init_db`` im Lifespan) oder ``alembic upgrade head``.
* Fehlen Spalten aus ``backend.db.NACHZUZIEHENDE_SPALTEN``? Dann steht das
  Schema noch nicht auf dem Stand, den die Basisrevision beschreibt — erst die
  Anwendung einmal starten lassen, dann stempeln.
* Steht schon eine Revision drin? Dieselbe -> nichts zu tun (idempotent). Eine
  andere -> abbrechen, das ist von Hand zu klaeren.

Es fasst **ausschliesslich** ``alembic_version`` an. Kein ``CREATE``, kein
``ALTER``, kein ``DROP`` auf einer Fachtabelle.

Der Aufruf in Produktion
------------------------
Die Falle ist bekannt und gemessen (2026-08-19): Die Dashboard-Variablen des
Jelastic-Knotens gelten fuer den **Dienstprozess**, eine SSH-Sitzung erbt sie
nicht. Ein Aufruf ueber den SSH-Gate steht deshalb ohne ``DATABASE_URL`` da und
bricht mit ``RuntimeError`` ab. Die URL wird darum mitgegeben — und zwar ueber
die **Standardeingabe**, damit sie weder in der Prozessliste des Knotens noch
in einer Shell-History steht::

    printf '%s\n' "$DATABASE_URL" | ssh -i ~/.ssh/deploy_key -p 3022 \
      213705-<ssh-user>@gate.jpc.infomaniak.com \
      'read -r DATABASE_URL && export DATABASE_URL && cd /home/jelastic/ROOT &&
        python scripts/alembic_stempeln.py --pruefen'

Erst mit ``--pruefen`` schauen, dann ohne den Schalter stempeln, dann noch
einmal mit ``--pruefen`` nachsehen. Die Ausgabe nennt jedes Mal Host und
Datenbanknamen (ohne Passwort) — daran, und nur daran, laesst sich hinterher
belegen, welche Datenbank angefasst wurde.

Aufruf lokal::

    python scripts/alembic_stempeln.py [--pruefen]

Rueckgabewerte: ``0`` alles in Ordnung, ``1`` es ist etwas zu tun oder etwas
stimmt nicht.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.db import get_engine  # noqa: E402
from backend.vault.models import Base  # noqa: E402
from scripts.schema_nachziehen import fehlende_spalten, ziel_beschreibung  # noqa: E402

#: Alembics Buchfuehrungstabelle.
ALEMBIC_TABELLE = "alembic_version"

#: Die ``alembic.ini`` des Repositories — dieselbe, die auch die CLI liest.
ALEMBIC_INI = _ROOT / "alembic.ini"


def alembic_config() -> Config:
    """Laedt die Konfiguration aus ``alembic.ini`` im Repository-Wurzelverzeichnis.

    Returns
    -------
    Config
        Konfiguration mit absolutem ``script_location``, damit der Aufruf
        unabhaengig vom aktuellen Arbeitsverzeichnis funktioniert.
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return config


def kopfrevision() -> str:
    """Die Revision, auf die gestempelt wird.

    Returns
    -------
    str
        Die einzige Spitze des Migrationsgraphen.

    Raises
    ------
    RuntimeError
        Wenn es keine oder mehrere Spitzen gibt. Bei mehreren waere unklar,
        worauf gestempelt werden soll — das ist von Hand zu klaeren
        (``alembic merge``), nicht hier zu raten.
    """
    spitzen = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(spitzen) != 1:
        raise RuntimeError(
            f"Erwartet wird genau eine Kopfrevision, gefunden: {list(spitzen)}"
        )
    return spitzen[0]


def gestempelte_revision(engine) -> str | None:
    """Liest den aktuellen Stand aus ``alembic_version``.

    Parameters
    ----------
    engine
        Die zu befragende Engine.

    Returns
    -------
    str or None
        Die eingetragene Revision, oder ``None``, wenn die Tabelle fehlt oder
        leer ist.
    """
    if not inspect(engine).has_table(ALEMBIC_TABELLE):
        return None
    with engine.connect() as verbindung:
        return verbindung.execute(
            text(f"SELECT version_num FROM {ALEMBIC_TABELLE}")  # noqa: S608
        ).scalar_one_or_none()


def fehlende_tabellen(engine) -> list[str]:
    """Tabellen aus den Modellen, die diese Datenbank nicht hat.

    Parameters
    ----------
    engine
        Die zu befragende Engine.

    Returns
    -------
    list of str
        Sortierte Namen der fehlenden Tabellen; leer, wenn alle da sind.
    """
    vorhanden = set(inspect(engine).get_table_names())
    return sorted(set(Base.metadata.tables) - vorhanden)


def main(argv: list[str] | None = None) -> int:
    """Prueft den Zustand und stempelt, wenn er stempelbar ist.

    Parameters
    ----------
    argv
        Argumentliste; ``None`` bedeutet ``sys.argv[1:]``.

    Returns
    -------
    int
        Prozess-Rueckgabewert.
    """
    # Die Ausgabe unten benutzt Pfeile und Haken. Eine Windows-Konsole laeuft
    # als cp1252 und braeche beim ersten ``print`` mit ``UnicodeEncodeError``
    # ab — ausgerechnet bei einem Werkzeug, dessen Zweck die nachlesbare
    # Meldung ist. Unter pytest ist ``sys.stdout`` ersetzt und kennt
    # ``reconfigure`` nicht; dann bleibt es, wie es ist.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Alembic-Basisrevision stempeln.")
    parser.add_argument(
        "--pruefen",
        action="store_true",
        help="Nur melden, was der Fall ist, und nichts schreiben.",
    )
    args = parser.parse_args(argv)

    engine = get_engine()
    kopf = kopfrevision()
    print(f"→ Zieldatenbank {ziel_beschreibung(engine)}")
    print(f"→ Kopfrevision  {kopf}")

    stand = gestempelte_revision(engine)
    if stand == kopf:
        print(f"✅ Bereits gestempelt auf {kopf} — nichts zu tun.")
        return 0
    if stand is not None:
        print(
            f"::error::Diese Datenbank steht auf {stand}, nicht auf {kopf}. "
            "Das ist von Hand zu klaeren — hier wird nichts ueberschrieben."
        )
        return 1

    offen = fehlende_tabellen(engine)
    if len(offen) == len(Base.metadata.tables):
        print(
            "::error::Diese Datenbank ist leer. Eine leere Datenbank wird NICHT "
            "gestempelt: entweder die Anwendung einmal starten (init_db im "
            "Lifespan) oder `python -m alembic upgrade head`."
        )
        return 1
    if offen:
        print(f"::error::Es fehlen Tabellen: {offen}. Das Schema entspricht der ")
        print(
            "::error::Basisrevision nicht — erst die Anwendung starten lassen, "
            "dann erneut pruefen."
        )
        return 1

    spalten = fehlende_spalten(engine)
    if spalten:
        namen = [f"{tabelle}.{spalte}" for tabelle, spalte, _ in spalten]
        print(f"::error::Es fehlen Spalten: {namen}. Das Schema entspricht der ")
        print(
            "::error::Basisrevision nicht — `backend.db.ensure_schema` laeuft im "
            "Lifespan, also die Anwendung einmal starten lassen."
        )
        return 1

    print(f"→ stempelbar: alle Tabellen und Spalten da, {ALEMBIC_TABELLE} leer.")
    if args.pruefen:
        print("→ --pruefen: nichts geschrieben.")
        return 1

    command.stamp(alembic_config(), kopf)

    nachher = gestempelte_revision(engine)
    if nachher != kopf:
        print(f"::error::Nach dem Stempeln steht dort {nachher!r} statt {kopf!r}.")
        return 1
    print(f"✅ Gestempelt: {ALEMBIC_TABELLE} = {nachher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
