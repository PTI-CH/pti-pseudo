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
  ein Wettlauf um dieselbe Alembic-Version. Migrationen laufen als eigener,
  ausgeloester Schritt im Deploy, nicht als Nebenwirkung eines Prozessstarts.
* **Schemaaenderungen laufen als idempotente Migration, nie als
  ``drop``/``create``.** Ein ``drop_all`` auf dieser Datenbank vernichtet die
  Zuordnungen unwiederbringlich; ein Restore aus dem Backup bringt sie nur bis
  zum letzten Dump zurueck. ``create_all`` legt ausschliesslich Fehlendes an und
  fasst bestehende Tabellen nicht an — bereits vorhandene Spalten aendert es
  aber auch nicht. Genau dafuer ist Alembic da.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine
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
    "Base",
    "create_db_engine",
    "get_database_url",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "reset_engine_cache",
]
