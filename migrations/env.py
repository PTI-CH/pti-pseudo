"""Alembic-Laufzeitumgebung: verbindet die Migrationen mit dieser Anwendung.

Zwei Dinge macht diese Datei, und beide sind bewusst so verdrahtet:

1. **Die Datenbank-URL kommt aus genau einer Quelle** —
   :func:`backend.db.get_database_url`. Das ist dieselbe Funktion, die die
   Anwendung benutzt, also auch dieselbe Reihenfolge (``backend.config`` vor
   ``DATABASE_URL``) und dieselbe URL samt ``?sslmode=require``: Die
   Jelastic-PostgreSQL erzwingt SSL ueber ``hostssl`` in ``pg_hba``, und eine
   URL ohne den Parameter kommt dort nicht an. In ``alembic.ini`` steht
   deshalb **kein** ``sqlalchemy.url``; stuende er dort, gaebe es zwei
   Wahrheiten und die im Repository traege ein Passwort.

2. **Die Engine wird ueber :func:`backend.db.create_db_engine` gebaut**, nicht
   ueber ein eigenes ``create_engine``. Damit gilt auch hier die
   SQLite-Sonderbehandlung (``StaticPool``), ohne die eine
   In-Memory-Datenbank zwischen zwei Verbindungen leer waere.

Aufrufer koennen eine fertige ``Connection`` unter
``config.attributes["connection"]`` hinterlegen — den Weg nimmt der
Waechtertest (``backend/tests/test_alembic_basis.py``), damit er ohne
``DATABASE_URL`` in der Umgebung auskommt.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.db import create_db_engine, get_database_url  # noqa: E402
from backend.vault.models import Base  # noqa: E402

config = context.config

# Nur wenn Alembic ueber die CLI mit einer ``alembic.ini`` gestartet wurde.
# ``disable_existing_loggers=False`` ist kein Beiwerk: Die Vorgabe ``True``
# wuerde jeden bereits eingerichteten Logger stilllegen — darunter den
# ``pti``-Namensraum der Anwendung, falls Alembic je aus einem Prozess heraus
# laeuft, der ihn schon konfiguriert hat.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: Die Modelle sind die Referenz. ``--autogenerate`` vergleicht dagegen.
target_metadata = Base.metadata


def _kontext_optionen(dialektname: str) -> dict:
    """Optionen, die auf beiden Dialekten gelten sollen.

    ``compare_type`` ist eingeschaltet, weil ein stillschweigend geaenderter
    Spaltentyp sonst durch ein ``--autogenerate`` faellt und erst in
    Produktion auffaellt.

    ``render_as_batch`` nur auf SQLite: Dort kann man Spalten nicht per
    ``ALTER TABLE`` aendern, Alembic baut die Tabelle stattdessen um. Auf
    PostgreSQL waere das ein unnoetiger und gefaehrlicher Umweg — dort wird
    kein Tabellenumbau gebraucht.

    Parameters
    ----------
    dialektname
        Der SQLAlchemy-Dialektname, etwa ``"postgresql"`` oder ``"sqlite"``.

    Returns
    -------
    dict
        Zusatzoptionen fuer ``context.configure``.
    """
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "render_as_batch": dialektname == "sqlite",
    }


def run_migrations_offline() -> None:
    """Erzeugt das SQL, ohne die Datenbank zu beruehren (``--sql``).

    Ohne Verbindung, ohne Zugangsdaten: Alembic braucht von der URL nur den
    Dialekt. Genau deshalb kann der Waechtertest damit PostgreSQL-DDL
    erzeugen, obwohl in der Testumgebung keine PostgreSQL laeuft.
    """
    url = config.get_main_option("sqlalchemy.url") or get_database_url()
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_kontext_optionen(url.split(":", 1)[0].split("+", 1)[0]),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Fuehrt die Migrationen gegen eine echte Verbindung aus."""
    verbindung = config.attributes.get("connection")
    if verbindung is not None:
        context.configure(
            connection=verbindung, **_kontext_optionen(verbindung.dialect.name)
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_db_engine(config.get_main_option("sqlalchemy.url") or None)
    with engine.connect() as verbindung:
        context.configure(
            connection=verbindung, **_kontext_optionen(verbindung.dialect.name)
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
