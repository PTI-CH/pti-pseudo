"""Tests fuer die Admin-Endpunkte der Schluesselverwaltung (``/api/v1/api-keys``).

Anders als die uebrigen Router aus Task 2 haengt dieser Router bewusst
**nicht** unter ``/api/ext/v1`` — ein Schluessel darf keine weiteren Schluessel
erzeugen oder widerrufen (Design-Spec Abschnitt 4.5, ``main.py``-Docstring zu
``DUAL_MOUNT_ROUTERS``). Geprueft wird das hier direkt: derselbe Pfad unter
``/api/ext/v1`` muss 404 liefern, nicht 401 oder 403 — es ist der Einhaengepunkt,
der fehlt, nicht die Authentisierung.

Die Fixture ist wie in ``test_api_key_auth.py`` bewusst kopiert statt
importiert: jede Testdatei soll ohne stille Kopplung an eine andere lesbar
bleiben.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Faehrt die Anwendung mit abgeschaltetem SSO (synthetischer Admin) hoch."""
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "b" * 64)

    from backend.config import get_settings
    from backend.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()

    from backend.main import create_app

    init_db()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def client_standard(client: TestClient) -> Iterator[TestClient]:
    """Dieselbe Anwendung, aber der synthetische Dev-Benutzer ist kein Admin.

    Bewusst **ohne** ``reset_engine_cache``: die In-Memory-Datenbank der
    ``client``-Fixture soll erhalten bleiben.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("DEV_ROLE", "standard")

    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        from backend.main import create_app

        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()


# ── Lebenszyklus ────────────────────────────────────────────────────────────


def test_lebenszyklus_erzeugen_nutzen_widerrufen(client: TestClient) -> None:
    """POST liefert den Klartext, GET nie — und der Token wirkt bis zum Widerruf."""
    erzeugt = client.post("/api/v1/api-keys", json={"label": "Testapp"})
    assert erzeugt.status_code == 201, erzeugt.text
    body = erzeugt.json()

    token = body["token"]
    assert token.startswith("ptps_")
    assert body["key_prefix"] == token[:12]
    assert body["label"] == "Testapp"
    assert body["active"] is True
    assert body["revoked_at"] is None
    assert body["expires_at"] is None
    assert body["last_used_at"] is None
    assert body["created_by"]
    assert body["created_at"]
    assert "id" in body

    # GET zeigt denselben Schluessel, aber nie den Klartext oder den Hash.
    liste = client.get("/api/v1/api-keys")
    assert liste.status_code == 200, liste.text
    eintraege = liste.json()["api_keys"]
    meiner = next(e for e in eintraege if e["id"] == body["id"])
    assert "token" not in meiner
    assert "key_hash" not in meiner
    assert meiner["active"] is True

    # Der Token wirkt auf dem Maschinenpfad.
    aufruf = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": token},
    )
    assert aufruf.status_code == 200, aufruf.text

    # Widerruf.
    widerruf = client.delete(f"/api/v1/api-keys/{body['id']}")
    assert widerruf.status_code == 200, widerruf.text
    assert widerruf.json() == {"revoked": True}

    # Der Token wirkt danach nicht mehr.
    danach = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": token},
    )
    assert danach.status_code == 401, danach.text
    assert danach.json()["detail"]["code"] == "API_KEY_INVALID"

    # GET zeigt den Schluessel jetzt als inaktiv, nicht geloescht.
    liste_danach = client.get("/api/v1/api-keys")
    meiner_danach = next(
        e for e in liste_danach.json()["api_keys"] if e["id"] == body["id"]
    )
    assert meiner_danach["active"] is False
    assert meiner_danach["revoked_at"] is not None

    # Widerruf ist idempotent: der zweite Aufruf bleibt 200.
    zweiter_widerruf = client.delete(f"/api/v1/api-keys/{body['id']}")
    assert zweiter_widerruf.status_code == 200, zweiter_widerruf.text
    assert zweiter_widerruf.json() == {"revoked": True}


def test_liste_neueste_zuerst(client: TestClient) -> None:
    """Sortierreihenfolge der Liste: neuester Schluessel zuerst."""
    erster = client.post("/api/v1/api-keys", json={"label": "Erster Sortiertest"})
    zweiter = client.post("/api/v1/api-keys", json={"label": "Zweiter Sortiertest"})
    assert erster.status_code == 201, erster.text
    assert zweiter.status_code == 201, zweiter.text

    eintraege = client.get("/api/v1/api-keys").json()["api_keys"]
    ids = [e["id"] for e in eintraege]
    assert ids.index(zweiter.json()["id"]) < ids.index(erster.json()["id"])


# ── Ablaufdatum ─────────────────────────────────────────────────────────────


def test_expires_at_in_der_zukunft_wird_uebernommen(client: TestClient) -> None:
    ablauf = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = client.post(
        "/api/v1/api-keys",
        json={"label": "Mit Ablaufdatum", "expires_at": ablauf},
    )
    assert r.status_code == 201, r.text
    assert r.json()["expires_at"] is not None


def test_expires_at_in_der_vergangenheit_400(client: TestClient) -> None:
    ablauf = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = client.post(
        "/api/v1/api-keys",
        json={"label": "Abgelaufen", "expires_at": ablauf},
    )
    assert r.status_code == 400, r.text


def test_expires_at_naiv_wird_als_utc_behandelt(client: TestClient) -> None:
    """Ein Ablaufdatum ohne Zeitzone gilt als UTC (wie ``models.as_utc``)."""
    naiv_in_der_vergangenheit = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).replace(tzinfo=None).isoformat()
    r = client.post(
        "/api/v1/api-keys",
        json={"label": "Naiv abgelaufen", "expires_at": naiv_in_der_vergangenheit},
    )
    assert r.status_code == 400, r.text


def test_zeitstempel_tragen_immer_eine_zonenangabe(client: TestClient) -> None:
    """Ohne Zone liest das Frontend einen ISO-String als Lokalzeit.

    Unter SQLite kommen die Werte naiv aus der Datenbank zurueck; ``isoformat``
    schriebe sie dann ohne ``+00:00``, und der Browser rechnete sie noch einmal
    um — im Sommer zwei Stunden daneben. Geprueft an der Liste, weil die Werte
    dort frisch gelesen und nicht aus dem Request uebernommen sind.
    """
    ablauf = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    erzeugt = client.post(
        "/api/v1/api-keys", json={"label": "Zeitzone", "expires_at": ablauf}
    )
    assert erzeugt.status_code == 201, erzeugt.text
    key_id = erzeugt.json()["id"]

    # Einmal benutzen, damit auch ``last_used_at`` gesetzt ist.
    client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": erzeugt.json()["token"]},
    )

    liste = client.get("/api/v1/api-keys")
    assert liste.status_code == 200, liste.text
    eintrag = next(k for k in liste.json()["api_keys"] if k["id"] == key_id)

    for feld in ("created_at", "expires_at", "last_used_at"):
        wert = eintrag[feld]
        assert wert is not None, feld
        assert wert.endswith("+00:00") or wert.endswith("Z"), (feld, wert)

    # Und derselbe Anspruch schon in der Antwort auf das Erzeugen.
    assert erzeugt.json()["created_at"].endswith("+00:00")


# ── Label-Validierung ────────────────────────────────────────────────────────


def test_label_grenzen(client: TestClient) -> None:
    """1..140 Zeichen.

    Die Zahl stammt aus der Zeit, als der Aktor ``apikey:<Bezeichnung>`` hiess
    und in ``usage_events.username`` (``String(150)``) passen musste. Seit der
    Attribution ueber die Schluessel-ID ist die Aktor-Laenge fest; die Grenze
    bleibt als harmlose Obergrenze fuer die Anzeige bestehen.
    """
    zu_lang = client.post("/api/v1/api-keys", json={"label": "x" * 141})
    assert zu_lang.status_code == 422, zu_lang.text

    genau_140 = client.post("/api/v1/api-keys", json={"label": "x" * 140})
    assert genau_140.status_code == 201, genau_140.text

    leer = client.post("/api/v1/api-keys", json={"label": "   "})
    assert leer.status_code == 422, leer.text


# ── Widerruf: fremder Mandant / unbekannt ───────────────────────────────────


def test_widerruf_unbekannte_id_404(client: TestClient) -> None:
    r = client.delete("/api/v1/api-keys/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404, r.text


def test_widerruf_ungueltige_id_400(client: TestClient) -> None:
    r = client.delete("/api/v1/api-keys/keine-uuid")
    assert r.status_code == 400, r.text


# ── Admin-Gate ───────────────────────────────────────────────────────────────


def test_ohne_adminrolle_403(client_standard: TestClient) -> None:
    erzeugt = client_standard.post("/api/v1/api-keys", json={"label": "Verboten"})
    assert erzeugt.status_code == 403, erzeugt.text
    assert erzeugt.json()["detail"] == (
        "Nur Administratoren duerfen API-Schluessel verwalten."
    )

    liste = client_standard.get("/api/v1/api-keys")
    assert liste.status_code == 403, liste.text

    loeschen = client_standard.delete(
        "/api/v1/api-keys/00000000-0000-0000-0000-000000000000"
    )
    assert loeschen.status_code == 403, loeschen.text


# ── Kein zweiter Einhaengepunkt ──────────────────────────────────────────────


def test_nicht_unter_ext_eingehaengt(client: TestClient) -> None:
    """Die Verwaltung ist nur ueber SSO erreichbar — auch mit gueltigem Schluessel.

    Es ist der Einhaengepunkt, der fehlt (404), nicht die Authentisierung
    (401/403) — deshalb der Aufruf mit einem gueltigen Token im Header.
    """
    token = client.post("/api/v1/api-keys", json={"label": "Fuer den Mount-Test"})
    assert token.status_code == 201, token.text

    r = client.get(
        "/api/ext/v1/api-keys", headers={"X-Api-Key": token.json()["token"]}
    )
    assert r.status_code == 404, r.text
