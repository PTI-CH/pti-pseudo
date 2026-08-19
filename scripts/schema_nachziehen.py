"""Zieht fehlende Spalten in einer bereits angelegten Datenbank nach.

Warum es dieses Skript gibt
---------------------------
``backend.db.init_db`` ruft ``create_all`` und legt damit ausschliesslich
**fehlende Tabellen** an. Eine neue Spalte in einem bestehenden Modell entsteht
dadurch nicht — die Tabelle existiert ja bereits. In Produktion faellt das nicht
beim Deploy auf, sondern beim ersten Zugriff: PostgreSQL antwortet mit
``column … does not exist``, und weil die Begriffsliste bei **jedem**
Pseudonymisierungslauf gelesen wird, waere das Werkzeug damit vollstaendig
ausser Betrieb.

Warum nicht Alembic
-------------------
Alembic ist das richtige Werkzeug und gehoert langfristig hierher. Es auf einer
bereits laufenden Produktionsdatenbank einzufuehren heisst aber, den Bestand
korrekt auf eine Basisversion zu stempeln — ein Schritt, der stimmen muss und
den man nicht nebenbei mit einer Funktionsaenderung ausliefert. Bis dahin dieser
Weg: ausdruecklich aufgerufen, idempotent, ohne jedes ``DROP``.

Die beiden Eigenschaften, auf die es ankommt:

* **Idempotent.** Jeder Lauf prueft erst den Ist-Zustand. Zweimal aufrufen
  aendert nichts, ein Deploy ohne Schemaaenderung laeuft durch.
* **Nur additiv.** Es werden ausschliesslich Spalten *hinzugefuegt*. Nichts
  wird geloescht, umbenannt oder umgetypt. Ein Fehler in dieser Datei kann
  damit keine Zuordnung vernichten — und die Zuordnungstabelle ist der einzige
  Datenbestand, dessen Verlust nicht zu reparieren ist.

Aufruf (im Deploy nach dem Upload, vor dem Neustart)::

    python scripts/schema_nachziehen.py

Beendet sich mit 0, wenn das Schema stimmt oder erfolgreich ergaenzt wurde,
sonst mit 1.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from sqlalchemy import inspect, text

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.db import get_engine  # noqa: E402

logger = logging.getLogger("pti.schema")

#: Nachzuziehende Spalten: ``(Tabelle, Spalte, SQL-Typ)``.
#:
#: Der Typ steht hier als SQL und nicht als SQLAlchemy-Typ, weil genau er im
#: ``ALTER TABLE`` landet — er soll ohne Uebersetzungsschritt nachlesbar sein
#: und zum Modell passen. Neue Eintraege kommen unten dazu und bleiben stehen;
#: eine Zeile, deren Spalte laengst existiert, kostet eine Pruefung.
NACHZUZIEHEN: tuple[tuple[str, str, str], ...] = (
    # 2026-08-19 — festes Pseudonym je Begriff (CustomTerm.replacement).
    ("custom_terms", "replacement", "VARCHAR(100)"),
)


def fehlende_spalten(engine) -> list[tuple[str, str, str]]:
    """Ermittelt, welche Eintraege aus :data:`NACHZUZIEHEN` wirklich fehlen.

    Ueber den Inspector statt ueber ``ADD COLUMN IF NOT EXISTS``: das
    ``IF NOT EXISTS`` kennt PostgreSQL, SQLite aber nicht — und die Tests
    laufen auf SQLite. Der Inspector arbeitet auf beiden gleich.

    Eine Tabelle, die es noch gar nicht gibt, wird uebersprungen: dann ist die
    Datenbank neu und ``init_db`` legt sie samt aller Spalten an.
    """
    inspector = inspect(engine)
    vorhandene_tabellen = set(inspector.get_table_names())

    offen: list[tuple[str, str, str]] = []
    for tabelle, spalte, sql_typ in NACHZUZIEHEN:
        if tabelle not in vorhandene_tabellen:
            logger.info(
                "Tabelle %s existiert noch nicht — init_db legt sie an.", tabelle
            )
            continue
        spalten = {s["name"] for s in inspector.get_columns(tabelle)}
        if spalte not in spalten:
            offen.append((tabelle, spalte, sql_typ))
    return offen


def ziel_beschreibung(engine) -> str:
    """Benennt die Zieldatenbank — ohne Passwort, aber eindeutig.

    Steht so im Deploy-Protokoll. Ohne diese Zeile liesse sich hinterher nicht
    belegen, *welche* Datenbank ein Lauf angefasst hat — und ein Lauf gegen die
    falsche haette hier gemeldet, alles sei in Ordnung.
    """
    url = engine.url
    if url.get_backend_name() == "sqlite":
        return f"sqlite: {url.database}"
    return f"{url.get_backend_name()}: {url.host}:{url.port}/{url.database}"


def main() -> int:
    """Zieht die fehlenden Spalten nach und meldet, was geschehen ist."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Ohne DATABASE_URL wirft ``get_engine`` — und genau so soll es sein: ein
    # Deploy, der die Datenbank nicht erreicht, muss stehen bleiben, bevor der
    # Knoten neu startet. Dann laeuft die alte Fassung weiter, statt dass eine
    # neue auf ein Schema trifft, das sie nicht kennt.
    engine = get_engine()
    print(f"→ Zieldatenbank {ziel_beschreibung(engine)}")

    offen = fehlende_spalten(engine)
    if not offen:
        print("✅ Schema ist aktuell — nichts nachzuziehen.")
        return 0

    for tabelle, spalte, sql_typ in offen:
        anweisung = f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {sql_typ}"
        print(f"→ {anweisung}")
        with engine.begin() as verbindung:
            verbindung.execute(text(anweisung))

    # Gegenprobe: nach dem Lauf darf nichts mehr offen sein. Ohne sie meldete
    # das Skript auch dann Erfolg, wenn die Datenbank das ALTER stillschweigend
    # verworfen haette.
    rest = fehlende_spalten(engine)
    if rest:
        print(f"::error::Spalten fehlen weiterhin: {rest}")
        return 1

    print(f"✅ {len(offen)} Spalte(n) nachgezogen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
