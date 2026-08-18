"""Tests fuer die HTTPS-Umleitung und die Security-Header.

Kern der Sache ist die Jelastic-Eigenheit: der Loadbalancer reicht Port 80
unverschluesselt durch und setzt ``X-Forwarded-Proto`` **nur bei HTTPS**. Ein
fehlender Header ist darum kein Beweis fuer eine lokale Verbindung, sondern im
Zweifel Klartext — die Tests halten genau das fest.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.core.https_redirect import HttpsRedirectMiddleware
from backend.routers import health


def make_settings(**overrides: Any) -> Settings:
    """Baut Settings, die weder Prozessumgebung noch .env-Datei lesen."""
    values: dict[str, Any] = {"_env_file": None, "env": "production"}
    values.update(overrides)
    return Settings(**values)


def client_for(**middleware_options: Any) -> TestClient:
    """Mini-App mit Health-Router und einer schreibenden Route."""
    app = FastAPI()
    app.include_router(health.router)

    @app.get("/dokumente")
    async def dokumente() -> dict[str, str]:
        return {"seite": "dokumente"}

    @app.post("/api/v1/dokumente")
    async def dokument_anlegen() -> dict[str, bool]:
        return {"angelegt": True}

    middleware_options.setdefault("settings", make_settings())
    app.add_middleware(HttpsRedirectMiddleware, **middleware_options)
    return TestClient(app, follow_redirects=False)


def test_produktion_ohne_forwarded_proto_leitet_um() -> None:
    """Fehlender Header heisst bei Jelastic Klartext — also 301."""
    response = client_for().get("/dokumente?id=7")

    assert response.status_code == 301
    assert response.headers["location"] == "https://testserver/dokumente?id=7"


def test_forwarded_proto_https_laesst_durch() -> None:
    """Der LB setzt den Header nur bei HTTPS — dann keine Umleitung."""
    response = client_for().get(
        "/dokumente", headers={"x-forwarded-proto": "https"}
    )

    assert response.status_code == 200
    assert response.json() == {"seite": "dokumente"}


def test_forwarded_proto_kommaliste_wertet_das_erste_element() -> None:
    """Bei mehreren Proxies zaehlt der aeusserste Eintrag."""
    durch = client_for().get(
        "/dokumente", headers={"x-forwarded-proto": "https, http"}
    )
    umgeleitet = client_for().get(
        "/dokumente", headers={"x-forwarded-proto": "http, https"}
    )

    assert durch.status_code == 200
    assert umgeleitet.status_code == 301


def test_health_wird_nicht_umgeleitet() -> None:
    """Sonst saehe der Deploy-Smoke-Test nur Redirects statt der Antwort."""
    response = client_for().get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_post_behaelt_die_methode_mit_308() -> None:
    """301 wuerde ein POST bei manchen Clients zu einem GET machen."""
    response = client_for().post("/api/v1/dokumente")

    assert response.status_code == 308
    assert response.headers["location"] == "https://testserver/api/v1/dokumente"


def test_head_wird_wie_get_mit_301_umgeleitet() -> None:
    """HEAD ist wie GET sicher und idempotent."""
    response = client_for().head("/dokumente")

    assert response.status_code == 301


def test_entwicklung_leitet_nicht_um() -> None:
    """Lokal ohne Proxy soll http einfach funktionieren."""
    response = client_for(settings=make_settings(env="development")).get("/dokumente")

    assert response.status_code == 200


def test_forwarded_host_bestimmt_das_redirect_ziel() -> None:
    """Hinter dem LB ist der interne Host nicht das, was der Browser kennt."""
    response = client_for().get(
        "/dokumente", headers={"x-forwarded-host": "pseudo.pti-tip-hub.ch"}
    )

    assert response.status_code == 301
    assert (
        response.headers["location"]
        == "https://pseudo.pti-tip-hub.ch/dokumente"
    )


def test_security_header_auf_durchgelassener_antwort() -> None:
    """Die Basis-Header haengen an jeder Antwort, HSTS nur ueber HTTPS."""
    response = client_for().get("/dokumente", headers={"x-forwarded-proto": "https"})

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert (
        response.headers["Strict-Transport-Security"]
        == "max-age=31536000; includeSubDomains"
    )


def test_kein_hsts_ueber_klartext() -> None:
    """Ueber HTTP ignorieren Browser HSTS ohnehin — also gar nicht erst senden."""
    response = client_for().get("/dokumente")

    assert response.status_code == 301
    assert "Strict-Transport-Security" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_security_header_auch_auf_health() -> None:
    """Die Ausnahme gilt nur fuer die Umleitung, nicht fuer die Header."""
    response = client_for().get("/api/health")

    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
