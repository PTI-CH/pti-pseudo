"""Zieht fehlende Spalten von Hand nach — dieselbe Logik wie beim App-Start.

Im Normalbetrieb braucht es dieses Skript **nicht**: ``backend.db.ensure_schema``
laeuft im Lifespan der Anwendung, also bei jedem Knoten-Neustart und damit bei
jedem Deploy. Dort steht ``DATABASE_URL`` garantiert zur Verfuegung.

Wozu es trotzdem da ist:

* **Nachschauen, ohne etwas zu starten.** ``--pruefen`` meldet, was fehlen
  wuerde, und aendert nichts.
* **Betrieb ausserhalb des Anwendungsprozesses** — etwa die Ein-Knoten-Variante
  mit SQLite (siehe JELASTIC_SETUP.md), ein Umzug oder eine Wiederherstellung
  aus dem Backup, bei der das Schema vor dem ersten Start stimmen soll.

Was es **nicht** kann: ueber den SSH-Gate laufen. Die Dashboard-Variablen des
Jelastic-Knotens gelten fuer den Dienstprozess, nicht fuer eine SSH-Sitzung —
am 2026-08-19 im Deploy nachgemessen, das Skript brach dort mit
``RuntimeError: DATABASE_URL ist nicht gesetzt`` ab. Wer es dort trotzdem
braucht, setzt die Variable im Aufruf selbst.

Die nachzuziehenden Spalten stehen in ``backend.db.NACHZUZIEHENDE_SPALTEN`` —
eine einzige Liste fuer beide Wege. Aufruf::

    python scripts/schema_nachziehen.py [--pruefen]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import inspect

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.db import (  # noqa: E402
    NACHZUZIEHENDE_SPALTEN,
    ensure_schema,
    get_engine,
)


def fehlende_spalten(engine) -> list[tuple[str, str, str]]:
    """Ermittelt, welche Eintraege aus der Liste in dieser Datenbank fehlen.

    Ueber den Inspector statt ueber ``ADD COLUMN IF NOT EXISTS``: das
    ``IF NOT EXISTS`` kennt PostgreSQL, SQLite aber nicht — und die Tests
    laufen auf SQLite. Der Inspector arbeitet auf beiden gleich.

    Eine Tabelle, die es noch gar nicht gibt, wird uebersprungen: dann ist die
    Datenbank neu und ``init_db`` legt sie samt aller Spalten an.
    """
    inspector = inspect(engine)
    vorhandene_tabellen = set(inspector.get_table_names())

    offen: list[tuple[str, str, str]] = []
    for tabelle, spalte, sql_typ in NACHZUZIEHENDE_SPALTEN:
        if tabelle not in vorhandene_tabellen:
            continue
        spalten = {s["name"] for s in inspector.get_columns(tabelle)}
        if spalte not in spalten:
            offen.append((tabelle, spalte, sql_typ))
    return offen


def ziel_beschreibung(engine) -> str:
    """Benennt die Zieldatenbank — ohne Passwort, aber eindeutig.

    Ohne diese Zeile liesse sich hinterher nicht belegen, *welche* Datenbank
    ein Lauf angefasst hat — und ein Lauf gegen die falsche haette gemeldet,
    alles sei in Ordnung.
    """
    url = engine.url
    if url.get_backend_name() == "sqlite":
        return f"sqlite: {url.database}"
    return f"{url.get_backend_name()}: {url.host}:{url.port}/{url.database}"


def main(argv: list[str] | None = None) -> int:
    """Zieht die fehlenden Spalten nach und meldet, was geschehen ist."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pruefen",
        action="store_true",
        help="Nur melden, was fehlt, und nichts aendern.",
    )
    args = parser.parse_args(argv)

    engine = get_engine()
    print(f"→ Zieldatenbank {ziel_beschreibung(engine)}")

    offen = fehlende_spalten(engine)
    if not offen:
        print("✅ Schema ist aktuell — nichts nachzuziehen.")
        return 0

    for tabelle, spalte, sql_typ in offen:
        print(f"→ fehlt: {tabelle}.{spalte} ({sql_typ})")
    if args.pruefen:
        return 1

    ergaenzt = ensure_schema(engine)
    rest = fehlende_spalten(engine)
    if rest:
        print(f"::error::Spalten fehlen weiterhin: {rest}")
        return 1

    print(f"✅ {len(ergaenzt)} Spalte(n) nachgezogen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
