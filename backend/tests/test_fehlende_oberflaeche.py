"""Die API bleibt eine API, auch wenn die Oberflaeche fehlt.

``backend/main.py`` haengt einen Auffang-Handler ein, wenn ``frontend/dist``
fehlt: Ein Seitenaufruf bekommt dann ein 503 mit dem Hinweis, dass die
Oberflaeche nicht mitgeliefert wurde. Der Handler ist als
``/{full_path:path}`` registriert und fing damit urspruenglich **auch**
``/api/``-Pfade — ein Tippfehler in einem Endpunkt ergab ein 503 mit einer
Meldung ueber das Frontend statt eines auswertbaren 404.

Fuer einen Maschinen-Aufrufer ist das der Unterschied zwischen „ich habe mich
vertippt" und „der Dienst ist kaputt".

Gefunden hat das am 2026-08-24 die neue Testpipeline: Sie baut fuer die
Backend-Tests kein Frontend. Lokal lag ``frontend/dist`` immer vor, deshalb
war der Fall hier nie sichtbar — dieser Test stellt ihn her, statt sich auf
die Umgebung zu verlassen.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

TEST_MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


@pytest.fixture
def client_ohne_oberflaeche(tmp_path) -> Iterator[TestClient]:
    """Baut die Anwendung so, als waere ``frontend/dist`` nicht vorhanden.

    ``FRONTEND_DIST`` zeigt dafuer auf ein leeres Verzeichnis — das ist der
    Zustand auf einem Knoten, dem der Deploy-Schritt „Frontend bauen" fehlt,
    und der Zustand in der CI.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", TEST_MASTER_KEY)

    import backend.main as main
    from backend.config import get_settings

    get_settings.cache_clear()
    mp.setattr(main, "FRONTEND_DIST", tmp_path / "gibt-es-nicht")
    try:
        with TestClient(main.create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()


def test_seitenaufruf_meldet_die_fehlende_oberflaeche(
    client_ohne_oberflaeche: TestClient,
) -> None:
    """Ein Mensch soll erfahren, was los ist — und was zu tun ist."""
    r = client_ohne_oberflaeche.get("/")
    assert r.status_code == 503
    assert r.json()["code"] == "FRONTEND_MISSING"


@pytest.mark.parametrize(
    "pfad",
    [
        "/api/v1/gibt-es-nicht",
        "/api/ext/v1/gibt-es-nicht",
        "/api/irgendwas",
    ],
)
def test_api_pfade_liefern_404_statt_der_frontend_meldung(
    client_ohne_oberflaeche: TestClient, pfad: str
) -> None:
    """Unter ``/api/`` bleibt ein unbekannter Endpunkt ein unbekannter Endpunkt.

    Ein 503 „Oberflaeche fehlt" waere fuer einen Maschinen-Aufrufer irrefuehrend:
    Er sucht dann einen Ausfall statt seines Tippfehlers.
    """
    r = client_ohne_oberflaeche.get(pfad)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "NOT_FOUND"


def test_health_bleibt_erreichbar(client_ohne_oberflaeche: TestClient) -> None:
    """Der Smoke-Test des Deploys haengt daran.

    Faellt Health mit aus, kann der Deploy nicht einmal melden, dass nur das
    Frontend fehlt.
    """
    r = client_ohne_oberflaeche.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
