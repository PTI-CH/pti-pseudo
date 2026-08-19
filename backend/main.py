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
from backend.routers import api_keys, documents, entities, health, me, terms, vaults

logger = logging.getLogger(__name__)

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

#: Die vier Router, die unter beiden Einhaengepunkten haengen. Nicht dabei:
#: ``me`` und ``health`` (kein Gegenstueck fuer Maschinen) und die
#: Schluesselverwaltung selbst (``api_keys``, siehe unten) — ein Schluessel
#: darf keine weiteren Schluessel erzeugen oder widerrufen.
DUAL_MOUNT_ROUTERS = (documents, entities, vaults, terms)


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
        from backend.db import init_db

        init_db()
        logger.info("Datenbankschema sichergestellt")
    except Exception:
        logger.exception("Datenbank nicht erreichbar — Vault-Funktionen fallen aus")

    yield

    # Der Introspektions-Client haelt offene Verbindungen zum CRM.
    try:
        from backend.core.hub_access_auth import _HTTP_CLIENT

        await _HTTP_CLIENT.aclose()
    except Exception:  # pragma: no cover — Abbau darf nie den Shutdown blockieren
        logger.debug("Schliessen des HTTP-Clients fehlgeschlagen", exc_info=True)


def create_app() -> FastAPI:
    """Baut die Anwendung zusammen."""
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
