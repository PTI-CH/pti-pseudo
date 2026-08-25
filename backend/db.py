"""Datenbank-Anbindung: Engine, Session-Factory und Schema-Anlage.

Eigene Datenbank, bewusst **nicht** die geteilte ``pti-db`` des CRM: der
Vault-Ciphertext und die Zuordnungstabelle gehoeren nicht in eine Datenbank, auf
die ein anderes Tool und andere Betriebsrollen Zugriff haben.

Schemaaenderungen
-----------------
``init_db`` legt fehlende Tabellen an und ist idempotent. Es ist **kein**
Migrationswerkzeug und ruft bewusst kein Alembic auf:

* Kein Alembic-Autorun beim Start. Bei zwei gunicorn-Workern starten zwei
  Prozesse gleichzeitig — ein automatisch angestossenes ``upgrade head`` waere
  ein Wettlauf um dieselbe Alembic-Version.
* **Schemaaenderungen laufen als idempotente Migration, nie als
  ``drop``/``create``.** Ein ``drop_all`` auf dieser Datenbank vernichtet die
  Zuordnungen unwiederbringlich; ein Restore aus dem Backup bringt sie nur bis
  zum letzten Dump zurueck. ``create_all`` legt ausschliesslich Fehlendes an und
  fasst bestehende Tabellen nicht an — bereits vorhandene Spalten aendert es
  aber auch nicht.

Fehlende **Spalten** zieht ``ensure_schema`` nach. Es laeuft ebenfalls im
Lifespan, aber unter einem PostgreSQL-Advisory-Lock, der genau den oben
beschriebenen Wettlauf aufloest — der zweite Worker wartet und findet danach
nichts mehr zu tun. Dass es im Prozess laeuft und nicht als Deploy-Schritt ueber
SSH, hat einen gemessenen Grund: Die Dashboard-Variablen des Knotens (darunter
``DATABASE_URL``) gelten fuer den Dienstprozess, nicht fuer eine SSH-Sitzung.
Am 2026-08-19 im Deploy nachgemessen — ein Skript ueber den SSH-Gate steht ohne
Datenbank da.

Alembic bleibt das richtige Werkzeug fuer alles, was ueber additive Spalten
hinausgeht; es auf einer laufenden Produktionsdatenbank einzufuehren heisst,
den Bestand korrekt auf eine Basisversion zu stempeln, und das gehoert nicht
nebenbei in eine Funktionsaenderung.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from backend.vault.models import Base

# -- Pool-Zuschnitt fuer 2 gunicorn-Worker ----------------------------------
# Jeder Worker haelt einen eigenen Pool; die Gesamtzahl der Verbindungen ist
# also (POOL_SIZE + MAX_OVERFLOW) * Worker = (5 + 5) * 2 = 20. Das liegt
# komfortabel unter den 100 Verbindungen, die eine PostgreSQL-Standardinstallation
# zulaesst, und laesst Luft fuer psql-Sitzungen und den Backup-Lauf.
POOL_SIZE = 5
MAX_OVERFLOW = 5
# Nach 30 Minuten wird eine Verbindung erneuert. Der Jelastic-LB und PostgreSQL
# raeumen leerlaufende Verbindungen selbst ab; ohne Recycling holt sich der Pool
# sonst irgendwann eine bereits tote Verbindung.
POOL_RECYCLE_SECONDS = 1800
# Wartezeit auf eine freie Verbindung, bevor die Anfrage mit einem Fehler
# abbricht. Lieber ein schneller 500er als ein haengender Request-Worker.
POOL_TIMEOUT_SECONDS = 30


def get_database_url() -> str:
    """Liest die Datenbank-URL aus ``backend.config``, sonst aus der Env.

    Import in der Funktion — siehe ``backend.vault.crypto._setting``: dieses
    Modul soll importierbar bleiben, auch wenn die Konfigurationsschicht noch
    nicht vollstaendig ist.
    """
    try:
        from backend.config import get_settings

        url = getattr(get_settings(), "database_url", None)
        if url:
            return str(url)
    except Exception:  # pragma: no cover — nur ohne/mit defekter Konfigschicht
        pass

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL ist nicht gesetzt. Erwartet wird eine PostgreSQL-URL "
            "mit psycopg-3-Treiber, z. B. postgresql+psycopg://benutzer:"
            "passwort@host:5432/pti_pseudonymisierer?sslmode=require "
            "(die Jelastic-PostgreSQL erzwingt SSL ueber hostssl in pg_hba)."
        )
    return url


def create_db_engine(url: str | None = None) -> Engine:
    """Baut eine Engine mit dem Pool-Zuschnitt oben.

    SQLite bekommt eine Sonderbehandlung: Fuer ``sqlite+pysqlite:///:memory:``
    (Testbetrieb) muss ``StaticPool`` erzwungen werden, sonst legt jede neue
    Verbindung eine **eigene, leere** In-Memory-Datenbank an und die gerade
    erzeugten Tabellen sind aus der naechsten Session heraus verschwunden.
    ``check_same_thread=False`` erlaubt zusaetzlich die Nutzung derselben
    Verbindung aus einem anderen Thread — bei einer In-Memory-DB ohne Nebenlauf
    unbedenklich.
    """
    url = url or get_database_url()

    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )

    return create_engine(
        url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        # Prueft eine Verbindung vor der Ausgabe aus dem Pool. Kostet einen
        # Roundtrip, verhindert aber den klassischen Fehler nach einem
        # DB-Neustart: der Pool haelt tote Verbindungen und jede Anfrage
        # scheitert, bis der Worker neu startet.
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Prozessweite Engine, beim ersten Zugriff erzeugt.

    Bewusst traege und nicht auf Modulebene: ein Import von ``backend.db`` soll
    keine Datenbank-Konfiguration voraussetzen (Werkzeuge, Tests, ``--help``).
    Ein Pool pro Prozess ist genau richtig — bei gunicorn heisst das ein Pool je
    Worker, und der Zuschnitt oben rechnet damit.
    """
    return create_db_engine()


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Session-Factory fuer den Anwendungsbetrieb.

    ``expire_on_commit=False``: nach dem Commit bleiben geladene Objekte lesbar,
    ohne dass SQLAlchemy sie nachlaedt. Sonst loest der Zugriff auf ein Attribut
    in der Antwort-Serialisierung eine weitere Abfrage aus — oder scheitert,
    wenn die Session bereits geschlossen ist.

    ``autoflush=False``: der Vault-Dienst flusht an den Stellen selbst, an denen
    es fachlich noetig ist. Ein impliziter Flush mitten in einer Leseabfrage
    macht Reihenfolgefehler schwer auffindbar.
    """
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


# -- Nachtrag fehlender Spalten ---------------------------------------------
#
# ``create_all`` legt fehlende TABELLEN an, aber keine fehlenden SPALTEN. Eine
# neue Spalte in einem bestehenden Modell entsteht in einer laufenden Datenbank
# also nicht — und sie faellt nicht beim Deploy auf, sondern beim ersten
# Zugriff (``column … does not exist``). Weil die Begriffsliste bei jedem
# Pseudonymisierungslauf gelesen wird, waere das Werkzeug damit ausser Betrieb.

#: Nachzuziehende Spalten: ``(Tabelle, Spalte, SQL-Typ)``.
#:
#: Der Typ steht als SQL und nicht als SQLAlchemy-Typ, weil genau er im
#: ``ALTER TABLE`` landet — er soll ohne Uebersetzungsschritt nachlesbar sein
#: und zum Modell passen. Neue Eintraege kommen dazu und bleiben stehen; eine
#: Zeile, deren Spalte laengst existiert, kostet eine Pruefung.
#:
#: **Nur additiv.** Hier stehen ausschliesslich Spalten, die *hinzukommen*.
#: Nichts wird geloescht, umbenannt oder umgetypt — ein Fehler an dieser Stelle
#: kann damit keine Zuordnung vernichten, und die Zuordnungstabelle ist der
#: einzige Datenbestand, dessen Verlust nicht zu reparieren ist.
NACHZUZIEHENDE_SPALTEN: tuple[tuple[str, str, str], ...] = (
    # 2026-08-19 — festes Pseudonym je Begriff (CustomTerm.replacement).
    ("custom_terms", "replacement", "VARCHAR(100)"),
    # 2026-08-25 — asynchrone Auftraege (Job).
    ("jobs", "eingabe_blob", "BYTEA"),
    ("jobs", "dateiname", "VARCHAR(255)"),
    ("jobs", "parameter_json", "JSONB"),
    ("jobs", "ergebnis_blob", "BYTEA"),
    ("jobs", "fortschritt", "INTEGER"),
    ("jobs", "gesamt", "INTEGER"),
    ("jobs", "fehler", "TEXT"),
    ("jobs", "aktor", "VARCHAR(200)"),
    ("jobs", "updated_at", "TIMESTAMPTZ"),
    ("jobs", "freigaben", "INTEGER NOT NULL DEFAULT 0"),
)

#: Schluessel des PostgreSQL-Advisory-Locks, der den Nachtrag serialisiert.
#: Frei gewaehlt, muss nur projektweit derselbe sein.
_SCHEMA_LOCK_KEY = 8_150_419


def ensure_schema(engine: Engine | None = None) -> list[tuple[str, str]]:
    """Zieht fehlende Spalten aus :data:`NACHZUZIEHENDE_SPALTEN` nach.

    Warum das hier laeuft und nicht als Deploy-Schritt ueber SSH: Die
    Umgebungsvariablen des Knotens (darunter ``DATABASE_URL``) setzt das
    Jelastic-Dashboard fuer den **Dienstprozess**. Eine SSH-Sitzung erbt sie
    nicht — ein Skript ueber den SSH-Gate steht ohne Datenbank da. Am
    2026-08-19 im Deploy nachgemessen: ``RuntimeError: DATABASE_URL ist nicht
    gesetzt``. Der Anwendungsprozess hat sie dagegen immer.

    Der Wettlauf zweier gunicorn-Worker, der gegen einen Autorun beim Start
    spricht (siehe Modulkopf), wird hier durch einen **Transaktions-Advisory-
    Lock** aufgeloest: Der zweite Worker wartet, findet danach nichts mehr zu
    tun und laeuft weiter. Der Lock faellt mit dem Ende der Transaktion, auch
    wenn der Prozess dazwischen stirbt. SQLite braucht ihn nicht — dort gibt es
    nur einen Schreiber.

    Bleibt es bei ``ALTER TABLE ... ADD COLUMN``: idempotent, rein additiv,
    ohne ``DROP`` und ohne ``RENAME``.

    :return: Die tatsaechlich ergaenzten ``(Tabelle, Spalte)`` — leer, wenn das
        Schema bereits stimmte.
    """
    engine = engine or get_engine()
    ergaenzt: list[tuple[str, str]] = []

    with engine.begin() as verbindung:
        if verbindung.dialect.name == "postgresql":
            verbindung.execute(
                text("SELECT pg_advisory_xact_lock(:schluessel)"),
                {"schluessel": _SCHEMA_LOCK_KEY},
            )

        inspector = inspect(verbindung)
        vorhandene_tabellen = set(inspector.get_table_names())

        for tabelle, spalte, sql_typ in NACHZUZIEHENDE_SPALTEN:
            if tabelle not in vorhandene_tabellen:
                # Neue Datenbank: ``create_all`` legt die Tabelle samt aller
                # Spalten an, hier ist nichts nachzutragen.
                continue
            spalten = {s["name"] for s in inspector.get_columns(tabelle)}
            if spalte in spalten:
                continue
            verbindung.execute(
                text(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {sql_typ}")
            )
            ergaenzt.append((tabelle, spalte))

    return ergaenzt


def init_db(engine: Engine | None = None) -> None:
    """Legt fehlende Tabellen an. Idempotent, mehrfach aufrufbar.

    ``create_all`` prueft je Tabelle auf Existenz und ueberspringt vorhandene.
    Es aendert bestehende Tabellen aber **nicht** — eine neue Spalte in einem
    Modell entsteht dadurch nicht in einer bereits angelegten Tabelle. Dafuer
    gibt es Alembic; siehe Modulkopf.

    Wird nicht automatisch beim Import ausgefuehrt. Der Aufruf gehoert in den
    Lifespan-Hook der Anwendung oder in ein Deploy-Skript.
    """
    Base.metadata.create_all(bind=engine or get_engine())


def reset_engine_cache() -> None:
    """Verwirft die zwischengespeicherte Engine und Session-Factory.

    Gedacht fuer Tests und fuer den seltenen Fall einer Konfigurationsaenderung
    zur Laufzeit. Bestehende Verbindungen werden dabei geschlossen.
    """
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


__all__ = [
    "NACHZUZIEHENDE_SPALTEN",
    "Base",
    "create_db_engine",
    "ensure_schema",
    "get_database_url",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "reset_engine_cache",
]
