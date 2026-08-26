"""Einstiegspunkt der Anwendung.

Startbefehl in Produktion (siehe ``deploy/run.sh``):

    gunicorn --config deploy/gunicorn.conf.py backend.main:app

Lokal:

    HUB_AUTH=off uvicorn backend.main:app --reload --port 8000

Die Reihenfolge der Middlewares ist bedeutungstragend. Starlette fuehrt die
zuletzt hinzugefuegte ZUERST aus, deshalb wird hier von innen nach aussen
registriert:

    1. HubAccessAuth    (innen — braucht schon HTTPS)
    2. HttpsRedirect    (aussen — muss vor allem anderen umleiten)

Ein unangemeldeter Aufruf ueber Klartext-HTTP soll erst auf HTTPS umgeleitet
werden und dann zum Hub — nicht umgekehrt, sonst landet der Benutzer nach dem
Login wieder auf einer ungesicherten Verbindung.
"""

from __future__ import annotations

import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.config import get_settings
from backend.core.api_key_auth import require_api_key
from backend.core.https_redirect import HttpsRedirectMiddleware
from backend.core.hub_access_auth import HubAccessAuthMiddleware
from backend.routers import (
    api_keys,
    documents,
    entities,
    health,
    jobs,
    me,
    terms,
    vaults,
)

#: Gemeinsamer Namensraum aller Logger dieses Baums.
#:
#: Eine Konfiguration erreicht nur, was unter ihrem Namen haengt — deshalb
#: heisst hier JEDER Logger `pti.…`, auch die in `main.py` und
#: `core/api_key_auth.py`, die frueher `__name__` benutzten und damit
#: `backend.…` hiessen. `test_protokoll.py` bewacht das.
PROTOKOLL_NAMENSRAUM = "pti"

#: Kennzeichen des eigenen Behandlers — siehe :func:`richte_protokoll_ein`.
BEHANDLER_NAME = "pti-protokoll"

#: Stufe und Herkunft in jeder Zeile. Ohne sie ist eine Meldung im gemeinsamen
#: Protokoll des Knotens nicht von einer des Webservers zu unterscheiden.
PROTOKOLL_FORMAT = "%(levelname)s [%(name)s] %(message)s"

logger = logging.getLogger("pti.main")


def richte_protokoll_ein() -> None:
    """Sorgt dafuer, dass die Meldungen dieser Anwendung ueberhaupt ankommen.

    **Warum es das braucht.** Bis zum 2026-08-25 konfigurierte die Anwendung
    ihr Protokoll gar nicht. Uvicorn richtet nur seine eigenen Logger ein und
    laesst den Wurzel-Logger unberuehrt; `pti.*` fand deshalb keinen Behandler
    und fiel auf Pythons ``lastResort`` zurueck — und der gibt nur WARNING und
    hoeher aus. Auf dem Knoten nachgemessen: `logger.warning("Schema
    ergaenzt…")` stand im Protokoll, `logger.info("Erkennungs-Engine
    geladen…")` null Mal.

    Das kostete Daten, nicht nur Bequemlichkeit: Die Messung zu Ort und
    Organisation schreibt mit `logger.info` und sammelte seit ihrem Deploy
    nichts — obwohl sie ausdruecklich als „erst mitmessen, dann entscheiden"
    gebaut wurde.

    **Warum auf stdout.** Der Jelastic-Runner faengt die Standardausgabe des
    Dienstprozesses ein und schreibt sie nach ``/home/jelastic/log/
    pythonengine.log``; dort liegt bereits die Rotationsregel. Eine eigene
    Datei waere eine zweite Stelle, die niemand rotiert.

    **Warum der Name in jeder Zeile steht.** Ohne ihn ist eine Meldung nicht
    zuzuordnen — genau daran waere die Untersuchung am 2026-08-25 fast
    gescheitert: „Schema ergaenzt" stand nackt da, ohne Stufe und ohne
    Herkunft, ununterscheidbar von einer Zeile des Webservers.

    Idempotent: Bei zwei uvicorn-Workern laeuft ``create_app`` zweimal im
    selben Prozessbaum; ein zweiter Behandler stuende sonst jede Zeile
    doppelt ins Protokoll.
    """
    logger_wurzel = logging.getLogger(PROTOKOLL_NAMENSRAUM)
    stufe = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logger_wurzel.setLevel(stufe)

    # Gesucht wird der EIGENE Behandler, nicht irgendeiner: Unter pytest haengt
    # die Testumgebung ihre eigenen an denselben Logger, und ein blosses
    # `if handlers:` liesse unseren dann nie entstehen.
    if any(getattr(h, "name", None) == BEHANDLER_NAME for h in logger_wurzel.handlers):
        # Schon eingerichtet — die Stufe oben ist aufgefrischt, sonst nichts tun.
        return

    behandler = logging.StreamHandler(sys.stdout)
    behandler.set_name(BEHANDLER_NAME)
    behandler.setFormatter(logging.Formatter(PROTOKOLL_FORMAT))
    logger_wurzel.addHandler(behandler)
    # Nicht an die Wurzel weiterreichen: Richtet spaeter jemand dort einen
    # Behandler ein, stuende sonst jede Zeile zweimal da.
    logger_wurzel.propagate = False

#: Wurzel des Repositories — von hier aus wird das gebaute Frontend gesucht.
REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

#: Pfade ohne SSO. Zwei Ausnahmen, jede mit einem eigenen Grund:
#:
#: * ``/api/health`` — die einzige Route, die der Smoke-Test und der
#:   Hub-Health-Poller ohne Sitzung erreichen koennen muessen.
#: * ``/api/ext/`` — der Maschinenpfad. Er hat kein Cookie und kann sich nicht
#:   am Hub anmelden; statt des Gates traegt er die Router-Abhaengigkeit
#:   ``require_api_key`` (siehe ``create_app``). Ausgenommen heisst hier also
#:   **nicht** ungeschuetzt, sondern anders geschuetzt.
EXEMPT_PATHS = [re.compile(r"^/api/health$"), re.compile(r"^/api/ext/")]

#: Pfade ohne HTTPS-Umleitung — bewusst eine **kuerzere** Liste als oben.
#: Der Maschinenpfad soll sehr wohl umgeleitet werden: ein Schluessel im Header
#: ueber Klartext-HTTP ist bereits einmal ungeschuetzt unterwegs gewesen, und
#: die Umleitung ist der letzte Hinweis darauf, dass die Doku "nur https" meint
#: (Design-Spec Abschnitt 5).
HTTPS_EXEMPT_PATHS = [re.compile(r"^/api/health$")]

#: Die Router, die unter beiden Einhaengepunkten haengen. Nicht dabei:
#: ``me`` und ``health`` (kein Gegenstueck fuer Maschinen) und die
#: Schluesselverwaltung selbst (``api_keys``, siehe unten) — ein Schluessel
#: darf keine weiteren Schluessel erzeugen oder widerrufen.
DUAL_MOUNT_ROUTERS = (documents, entities, vaults, terms, jobs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startet und beendet die teuren Ressourcen genau einmal je Prozess."""
    settings = get_settings()

    # Das spaCy-Modell braucht rund 7 Sekunden und 400 MB. Es hier zu laden
    # bedeutet: gunicorn mit preload_app zieht es EINMAL im Master hoch, die
    # Worker erben es per Copy-on-Write. Laedt man es faul beim ersten Request,
    # zahlt jeder Worker den vollen Preis erneut.
    try:
        from backend.engine.analyzer import get_default_analyzer

        get_default_analyzer()
        logger.info("Erkennungs-Engine geladen (Modell %s)", settings.spacy_model)
    except Exception:
        # Bewusst nicht fatal: /api/health muss antworten koennen, damit der
        # Smoke-Test einen klaren Befund liefert statt einer toten Verbindung.
        logger.exception("Erkennungs-Engine konnte nicht geladen werden")

    try:
        from backend.db import ensure_schema, init_db

        init_db()
        # Fehlende Spalten nachziehen. ``init_db`` legt nur fehlende TABELLEN
        # an — eine neue Spalte faellt sonst erst beim ersten Zugriff auf, und
        # weil die Begriffsliste bei jedem Lauf gelesen wird, waere das
        # Werkzeug damit ausser Betrieb. Serialisiert ueber einen
        # Advisory-Lock, siehe ``backend.db.ensure_schema``.
        ergaenzt = ensure_schema()
        if ergaenzt:
            logger.warning(
                "Schema ergaenzt: %s",
                ", ".join(f"{tabelle}.{spalte}" for tabelle, spalte in ergaenzt),
            )
        logger.info("Datenbankschema sichergestellt")
    except Exception:
        logger.exception("Datenbank nicht erreichbar — Vault-Funktionen fallen aus")

    # Einmal aufraeumen — und zwar BEVOR und UNABHAENGIG davon, ob der
    # Arbeiter gleich startet.
    #
    # Der Aufraeumlauf lebt sonst ausschliesslich im Arbeiterfaden (jeder
    # 150. Durchgang). Startet der Arbeiter nicht, raeumt also niemand: Die
    # 503-Schranke in ``warteschlange.lege_an`` deckt den HAEUFIGEN Grund ab
    # (falscher Dialekt — dann entsteht gar keine Zeile), aber nicht den
    # seltenen, gegen den der ``except`` unten steht: ein Importfehler oder
    # eine beim Start nicht erreichbare Datenbank. Dann nimmt ``lege_an``
    # Auftraege weiterhin an, und jede angenommene Zeile traegt die
    # verschluesselte Eingabedatei mit sich.
    #
    # Zusammen mit der Verfallszeit ab Anlage (siehe ``lege_an``) genuegt
    # das: Jeder Prozessstart raeumt dann nach, und ein Neustart kommt auf
    # diesem Knoten spaetestens mit dem naechsten Deploy.
    #
    # Der Freigabe-Schritt im Aufraeumlauf ist dabei kein Beiwerk: ``raeume_ab``
    # verschont ``laeuft``-Zeilen grundsaetzlich, eine haengengebliebene Zeile
    # wird also ueberhaupt erst durch ``gib_verwaiste_frei`` je abraeumbar.
    try:
        from backend.db import get_sessionmaker
        from backend.jobs import warteschlange

        with get_sessionmaker()() as session:
            warteschlange_aufgeraeumt = warteschlange.aufraeumlauf(session)
        if warteschlange_aufgeraeumt:
            logger.info(
                "Beim Start entfernt: %d abgelaufene Auftraege",
                warteschlange_aufgeraeumt,
            )
    except Exception:
        # Wie ueberall im Lifespan: nicht fatal. Ein nicht ausgefuehrter
        # Aufraeumlauf verzoegert nur, er beschaedigt nichts.
        logger.warning("Aufraeumen beim Start fehlgeschlagen", exc_info=True)

    # Der Auftragsarbeiter. Je uvicorn-Worker einer; die Zuteilung regelt die
    # Datenbank, sonst bekaemen beide denselben Auftrag.
    #
    # Nur auf PostgreSQL: dort teilen sich die Sitzungen KEINE Verbindung,
    # jede Sitzung holt sich eine eigene aus dem Pool. Auf SQLite (Testbetrieb,
    # ``StaticPool``) teilen sich dagegen ALLE Sitzungen eine einzige
    # zugrunde liegende Verbindung — ein Faden, der daneben pollt, zerlegt
    # deren Transaktionszustand fuer alle anderen. Gemessen am 2026-08-25:
    # mit gestartetem Faden wurde ``test_api_key_auth.py`` sporadisch rot
    # (ein direkt eingefuegter Schluessel war fuer die naechste Anfrage nicht
    # sichtbar), obwohl derselbe Test allein zuverlaessig gruen lief — dasselbe
    # Fehlerbild, das ``warteschlange.hole_naechsten`` fuer den SQLite-Zweig
    # schon dokumentiert. ``hintergrundverarbeitung_moeglich`` traegt dieselbe
    # Bedingung; ``warteschlange.lege_an`` prueft sie ebenfalls und weist eine
    # grosse Datei auf diesem Dialekt mit 503 ab, statt eine Auftragszeile
    # anzulegen, die dieser Faden nie abarbeiten wuerde.
    arbeiter = None
    try:
        from backend.db import get_engine, get_sessionmaker
        from backend.jobs.arbeiter import Arbeiter
        from backend.jobs.ausfuehrung import fuehre_aus
        from backend.jobs.warteschlange import hintergrundverarbeitung_moeglich

        dialekt = get_engine().dialect.name
        if hintergrundverarbeitung_moeglich(dialekt):
            arbeiter = Arbeiter(get_sessionmaker(), fuehre_aus)
            arbeiter.start()
        else:
            # Nicht stumm bleiben: ``lege_an`` weist grosse Dateien auf
            # diesem Dialekt zwar bereits mit einer klaren 503-Meldung ab
            # (niemand wartet auf einen Auftrag, der nie startet) — aber wer
            # den Knoten betreibt, soll trotzdem im Protokoll sehen, WARUM,
            # nicht nur, dass Uploads oberhalb der Schwelle scheitern.
            logger.warning(
                "Auftragsarbeiter nicht gestartet: Dialekt %s. Auftraege "
                "bleiben liegen. Hintergrundverarbeitung braucht PostgreSQL "
                "— auf SQLite teilen sich alle Sitzungen eine Verbindung, "
                "und ein Faden daneben zerlegt deren Zustand.",
                dialekt,
            )
    except Exception:
        # Wie beim spaCy-Modell: nicht fatal. Ohne Arbeiter bleiben Auftraege
        # liegen, aber /api/health und der synchrone Pfad antworten weiter.
        logger.warning("Auftragsarbeiter konnte nicht starten", exc_info=True)

    try:
        yield
    finally:
        if arbeiter is not None:
            arbeiter.stopp()

        # Der Introspektions-Client haelt offene Verbindungen zum CRM.
        try:
            from backend.core.hub_access_auth import _HTTP_CLIENT

            await _HTTP_CLIENT.aclose()
        except Exception:  # pragma: no cover — Abbau darf nie blockieren
            logger.debug("Schliessen des HTTP-Clients fehlgeschlagen", exc_info=True)


def create_app() -> FastAPI:
    """Baut die Anwendung zusammen."""
    # Zuerst das Protokoll: Was hier folgt, soll sichtbar sein — auch die
    # Meldungen des Lifespans, der gleich unten eingehaengt wird.
    richte_protokoll_ein()

    app = FastAPI(
        title="PTI-Pseudonymisierer",
        version=__version__,
        description=(
            "Erkennt Personendaten in Texten und Dokumenten, ersetzt sie durch "
            "Pseudonyme und fuehrt sie auf Wunsch wieder zurueck."
        ),
        lifespan=lifespan,
        # Die interaktive Doku bleibt hinter dem SSO-Gate — sie ist kein
        # oeffentliches Angebot, aber fuer angemeldete Entwickler nuetzlich.
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Reihenfolge siehe Modul-Docstring: zuletzt hinzugefuegt laeuft zuerst.
    # Welches Flag geprueft wird, steht in settings.hub_app_key (HUB_APP_KEY).
    app.add_middleware(HubAccessAuthMiddleware, exempt=EXEMPT_PATHS)
    app.add_middleware(HttpsRedirectMiddleware, exempt=HTTPS_EXEMPT_PATHS)

    app.include_router(health.router)
    app.include_router(me.router)

    # Dieselben Router zweimal: der Menschenpfad hinter dem SSO-Gate, der
    # Maschinenpfad hinter der Schluessel-Pruefung. Die Handler kennen den
    # Unterschied nicht — sie fragen ueber ``get_tenant``/``current_actor``
    # nach Mandant und Aktor, und die Antwort haengt daran, ob
    # ``require_api_key`` einen Schluessel hinterlegt hat.
    for prefix, dependencies in (
        ("/api/v1", []),
        ("/api/ext/v1", [Depends(require_api_key)]),
    ):
        for modul in DUAL_MOUNT_ROUTERS:
            app.include_router(modul.router, prefix=prefix, dependencies=dependencies)

    # Nur unter dem Menschenpfad, hinter dem SSO-Gate — siehe
    # ``api_keys``-Modul-Docstring und ``DUAL_MOUNT_ROUTERS`` oben.
    app.include_router(api_keys.router, prefix="/api/v1")

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Liefert das gebaute React-SPA same-origin aus.

    Same-origin ist Absicht und der Grund, warum es hier kein CORS gibt: der
    Browser spricht nur mit dieser einen Herkunft, und das geteilte
    Sitzungs-Cookie wird ohne Sonderregeln mitgeschickt.

    Fehlt ``frontend/dist``, startet die Anwendung trotzdem — mit einer
    ehrlichen Erklaerung statt eines nackten 404. Das ist der wahrscheinlichste
    Deploy-Fehler auf dem Python-Knoten, weil ein Panel-"Update from Repository"
    nur den Quellcode holt und das Frontend nicht mitbaut.
    """
    if not (FRONTEND_DIST / "index.html").exists():
        logger.warning("frontend/dist fehlt — die Oberflaeche wird nicht ausgeliefert")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _missing_frontend(full_path: str) -> JSONResponse:
            """Sagt, dass die Oberflaeche fehlt — aber nur fuer Seitenaufrufe.

            ``/api/`` bleibt ausgenommen, genau wie im Normalfall unten. Sonst
            aendert eine fehlende Oberflaeche das Verhalten der **API**: Ein
            Tippfehler in einem Endpunkt bekaeme ein 503 mit einer Meldung
            ueber das Frontend statt eines auswertbaren 404. Ein Maschinen-
            Aufrufer haette damit keine Chance, den eigenen Fehler zu erkennen.

            Die Auslassung ist am 2026-08-24 von der neuen Testpipeline
            gefunden worden: Sie baut fuer die Backend-Tests kein Frontend, und
            drei Tests, die 404 auf nicht eingehaengten Routen pruefen, sahen
            plotzlich 503.
            """
            if full_path.startswith("api/"):
                return JSONResponse(
                    status_code=404,
                    content={"code": "NOT_FOUND", "message": "Unbekannter Endpunkt."},
                )
            return JSONResponse(
                status_code=503,
                content={
                    "code": "FRONTEND_MISSING",
                    "message": (
                        "Die Oberflaeche wurde nicht mitgeliefert "
                        "(frontend/dist fehlt). "
                        "Die API ist erreichbar. Auf dem Server: Deploy ueber die "
                        "GitHub-Action nachziehen — ein reines VCS-Update im Panel "
                        "baut das Frontend nicht."
                    ),
                },
            )

        return

    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):
        """Alles, was keine API-Route ist, bekommt die index.html.

        Client-seitiges Routing braucht das: ein direkter Aufruf einer
        Unterseite muss dieselbe Anwendung laden statt 404 zu liefern.
        Statische Einzeldateien (Logo, favicon) werden vorher durchgereicht.

        Ausgenommen ist alles unterhalb von ``/api/``: was dort ankommt, ist
        ein Tippfehler in einem API-Aufruf und keine Seite. Ein Aufrufer soll
        dafuer ein auswertbares 404 sehen und nicht 200 mit HTML — und beim
        ausgenommenen Maschinenpfad ``/api/ext/`` waere die Seitenhuelle sonst
        sogar ohne Anmeldung zu haben.
        """
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"code": "NOT_FOUND", "message": "Unbekannter Endpunkt."},
            )

        candidate = (FRONTEND_DIST / full_path).resolve()
        # Pfad-Ausbruch verhindern: nur Dateien unterhalb von dist ausliefern.
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(FRONTEND_DIST.resolve())
        ):
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


app = create_app()
