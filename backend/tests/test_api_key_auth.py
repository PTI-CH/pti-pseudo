"""Tests fuer die externe API unter ``/api/ext/v1`` und ihre Schluessel.

Geprueft wird der Maschinenpfad so, wie ihn eine andere PTI-Anwendung sieht:
ohne Cookie, ohne Hub, nur mit einem Schluessel im Header.

Zwei Dinge sind hier bewusst und nicht offensichtlich:

* Die Schluessel werden **direkt ueber das Modell** angelegt. Den
  Verwaltungs-Endpunkt gibt es erst in Task 3; dieser Test soll aber schon
  jetzt allein lauffaehig sein.
* ``HUB_AUTH=off`` schaltet nur den SSO-Pfad ab. Der ``/api/ext/``-Pfad
  verlangt auch lokal einen gueltigen Schluessel — sonst waere hier ein Test
  gruen, der in Produktion rot ist (Spec Abschnitt 5).

Die Fixture ist wie in ``test_integration_api.py`` bewusst kopiert statt
importiert: jede Testdatei soll ohne stille Kopplung an eine andere lesbar
bleiben.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

#: Testtext mit echten Umlauten. Frei erfunden — es gehoeren keine echten
#: Personendaten in dieses Repository.
TEXT = "Herr Andreas Meier wohnt in 8001 Zürich und schreibt später erneut."


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Faehrt die Anwendung mit abgeschaltetem SSO und In-Memory-DB hoch."""
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

    Ohne diese zweite App sind die Admin-Gate-Tests wertlos: unter
    ``DEV_ROLE=admin`` setzt die Middleware den synthetischen Admin auch auf
    ``/api/ext/`` (sie prueft ``hub_auth == "off"`` vor der Ausnahmeliste), und
    ``is_admin(request) or has_api_key(request)`` waere schon durch den ersten
    Teil erfuellt — der Test bliebe gruen, selbst wenn man ``has_api_key``
    ersatzlos entfernte. Mit ``DEV_ROLE=standard`` kann das Gate nur noch der
    Schluessel oeffnen.

    Bewusst **ohne** ``reset_engine_cache``: die In-Memory-Datenbank der
    ``client``-Fixture soll erhalten bleiben, damit ``_lege_schluessel_an`` und
    beide Apps denselben Bestand sehen.
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


def _lege_schluessel_an(
    label: str = "Testapp",
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> str:
    """Legt einen Schluessel direkt in der Datenbank an und liefert den Klartext.

    Der Klartext existiert nur hier und in der Antwort des (spaeteren)
    Verwaltungs-Endpunkts — gespeichert wird ausschliesslich der SHA-256.
    """
    from backend.core.api_key_auth import generate_token, hash_token, key_prefix_of
    from backend.core.deps import get_tenant
    from backend.db import get_sessionmaker
    from backend.vault.models import ApiKey

    token = generate_token()
    with get_sessionmaker()() as session:
        tenant = get_tenant(session)
        session.add(
            ApiKey(
                tenant_id=tenant.id,
                label=label,
                key_prefix=key_prefix_of(token),
                key_hash=hash_token(token),
                created_by="test",
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        )
        session.commit()
    return token


def _last_used_at(token: str) -> datetime | None:
    """Liest ``last_used_at`` frisch aus der Datenbank, immer zeitzonenbewusst.

    SQLite gibt den Zeitstempel naiv zurueck — ohne das Geradeziehen liesse er
    sich nicht mit ``datetime.now(timezone.utc)`` vergleichen.
    """
    from sqlalchemy import select

    from backend.core.api_key_auth import hash_token
    from backend.db import get_sessionmaker
    from backend.vault.models import ApiKey, _as_utc

    with get_sessionmaker()() as session:
        row = session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_token(token))
        ).scalar_one()
        return _as_utc(row.last_used_at)


def _setze_last_used_at(token: str, wert: datetime) -> None:
    """Stellt ``last_used_at`` kuenstlich zurueck, um die Drossel zu pruefen."""
    from sqlalchemy import select

    from backend.core.api_key_auth import hash_token
    from backend.db import get_sessionmaker
    from backend.vault.models import ApiKey

    with get_sessionmaker()() as session:
        row = session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_token(token))
        ).scalar_one()
        row.last_used_at = wert
        session.commit()


# ── Token-Format ───────────────────────────────────────────────────────────


def test_token_hat_praefix_und_laenge() -> None:
    """Das Praefix macht den Token in Logs und Secret-Scannern erkennbar."""
    from backend.core.api_key_auth import generate_token, key_prefix_of

    token = generate_token()
    assert token.startswith("ptps_")
    assert len(token) >= 40
    assert key_prefix_of(token) == token[:12]
    assert generate_token() != token, "zwei Aufrufe duerfen nie denselben Token liefern"


def test_hash_ist_sha256_hex_und_stabil() -> None:
    """Der Lookup laeuft ueber diesen Hash — er muss reproduzierbar sein."""
    import hashlib

    from backend.core.api_key_auth import hash_token

    assert hash_token("ptps_beispiel") == hashlib.sha256(b"ptps_beispiel").hexdigest()
    assert len(hash_token("ptps_beispiel")) == 64


# ── Authentisierung ────────────────────────────────────────────────────────


def test_ohne_header_401_mit_code(client: TestClient) -> None:
    """Fehlender Header ist ein anderer Befund als ein falscher Schluessel."""
    r = client.post("/api/ext/v1/documents:detect", json={"text": "Hallo"})
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "API_KEY_MISSING"


def test_falscher_token_401(client: TestClient) -> None:
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": "ptps_falsch"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "API_KEY_INVALID"


def test_gueltiger_token_per_x_api_key_und_bearer(client: TestClient) -> None:
    """Beide Header-Formen muessen wirken — Bearer ist der Standardweg."""
    token = _lege_schluessel_an(label="Header-Test")
    for headers in ({"X-Api-Key": token}, {"Authorization": f"Bearer {token}"}):
        r = client.post(
            "/api/ext/v1/documents:detect",
            json={"text": "Herr Andreas Meier"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert "PERSON" in {e["entity_type"] for e in r.json()["entities"]}


def test_widerrufener_token_401(client: TestClient) -> None:
    """Widerruf ist ein Soft-Delete — der Datensatz bleibt, der Zugang nicht."""
    token = _lege_schluessel_an(
        label="Widerrufen", revoked_at=datetime.now(timezone.utc)
    )
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "API_KEY_INVALID"


def test_abgelaufener_token_401(client: TestClient) -> None:
    token = _lege_schluessel_an(
        label="Abgelaufen",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "API_KEY_INVALID"


def test_zukuenftiges_ablaufdatum_bleibt_gueltig(client: TestClient) -> None:
    token = _lege_schluessel_an(
        label="Laeuft noch",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 200, r.text


def test_erster_aufruf_setzt_last_used_at(client: TestClient) -> None:
    """``last_used_at`` beantwortet die Frage 'wird der Schluessel noch benutzt?'."""
    from sqlalchemy import select

    from backend.core.api_key_auth import hash_token
    from backend.db import get_sessionmaker
    from backend.vault.models import ApiKey

    token = _lege_schluessel_an(label="Benutzt")
    client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Hallo"},
        headers={"X-Api-Key": token},
    )

    with get_sessionmaker()() as session:
        row = session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_token(token))
        ).scalar_one()
        assert row.last_used_at is not None


def test_last_used_at_wird_hoechstens_einmal_pro_minute_geschrieben(
    client: TestClient,
) -> None:
    """Ohne diese Drossel waere jeder API-Aufruf zusaetzlich ein UPDATE.

    Geprueft werden beide Haelften: innerhalb der Frist bleibt der Zeitstempel
    stehen, ausserhalb wird er nachgezogen. Nur die zweite Haelfte allein waere
    auch gruen, wenn gar nicht gedrosselt wuerde.
    """
    from backend.core.api_key_auth import LAST_USED_WRITE_INTERVAL_SECONDS

    token = _lege_schluessel_an(label="Gedrosselt")
    headers = {"X-Api-Key": token}
    anfrage = {"text": "Hallo"}

    assert (
        client.post("/api/ext/v1/documents:detect", json=anfrage, headers=headers)
    ).status_code == 200
    erster = _last_used_at(token)
    assert erster is not None

    assert (
        client.post("/api/ext/v1/documents:detect", json=anfrage, headers=headers)
    ).status_code == 200
    assert _last_used_at(token) == erster, "innerhalb der Frist darf nichts passieren"

    veraltet = datetime.now(timezone.utc) - timedelta(
        seconds=LAST_USED_WRITE_INTERVAL_SECONDS + 5
    )
    _setze_last_used_at(token, veraltet)

    assert (
        client.post("/api/ext/v1/documents:detect", json=anfrage, headers=headers)
    ).status_code == 200
    nachgezogen = _last_used_at(token)
    assert nachgezogen is not None and nachgezogen > veraltet


# ── Mandant des Schluessels ────────────────────────────────────────────────


def test_verschwundener_mandant_scheitert_laut_statt_still(
    client: TestClient,
) -> None:
    """Ein Schluessel ohne auffindbaren Mandanten darf nicht in den Default rutschen.

    Das war der gefaehrlichere der beiden moeglichen Wege: ein stiller
    Rueckfall haette die aufrufende Anwendung in einem fremden Bestand
    arbeiten lassen — Vaults dort anlegen, Begriffe dort aendern — und niemand
    haette es bemerkt. Der Fehler schlaegt deshalb bis nach oben durch.

    Die Fremdschluessel-Bedingung erzwingt SQLite ohne ``PRAGMA foreign_keys``
    nicht; der unmoegliche Zustand laesst sich hier also herstellen.
    """
    import uuid

    from backend.core.api_key_auth import generate_token, hash_token, key_prefix_of
    from backend.db import get_sessionmaker
    from backend.vault.models import ApiKey

    token = generate_token()
    with get_sessionmaker()() as session:
        session.add(
            ApiKey(
                tenant_id=uuid.uuid4(),  # existiert nicht
                label="Verwaist",
                key_prefix=key_prefix_of(token),
                key_hash=hash_token(token),
                created_by="test",
            )
        )
        session.commit()

    with pytest.raises(RuntimeError, match="Mandant des API-Schluessels"):
        client.post(
            "/api/ext/v1/documents:detect",
            json={"text": "Hallo"},
            headers={"X-Api-Key": token},
        )


# ── Fachliche Gleichwertigkeit von /api/v1 und /api/ext/v1 ─────────────────


def test_rundlauf_anonymize_reverse_ueber_ext_mit_text(client: TestClient) -> None:
    """Der Maschinenpfad kann alles, was die Oberflaeche kann — verlustfrei."""
    token = _lege_schluessel_an(label="Rundlauf")
    headers = {"X-Api-Key": token}

    r = client.post(
        "/api/ext/v1/documents:anonymize",
        json={"text": TEXT, "mode": "reversible", "vault_label": "Ext-Rundlauf"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vault_id"]
    assert "Andreas Meier" not in body["text"]

    zurueck = client.post(
        "/api/ext/v1/documents:reverse",
        json={"text": body["text"], "vault_id": body["vault_id"]},
        headers=headers,
    )
    assert zurueck.status_code == 200, zurueck.text
    assert zurueck.json()["text"] == TEXT


def test_rundlauf_anonymize_reverse_ueber_ext_mit_datei(client: TestClient) -> None:
    """Dieselbe Kette ueber den Dateipfad, hier mit einer schlichten TXT."""
    token = _lege_schluessel_an(label="Rundlauf-Datei")
    headers = {"X-Api-Key": token}

    r = client.post(
        "/api/ext/v1/documents:anonymize-file",
        files={"file": ("notiz.txt", TEXT.encode("utf-8"), "text/plain")},
        data={"mode": "reversible", "vault_label": "Ext-Datei"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ersetzt = base64.b64decode(body["content_base64"])
    assert b"Andreas Meier" not in ersetzt

    zurueck = client.post(
        "/api/ext/v1/documents:reverse-file",
        files={"file": ("notiz.txt", ersetzt, "text/plain")},
        data={"vault_id": body["vault_id"]},
        headers=headers,
    )
    assert zurueck.status_code == 200, zurueck.text
    wieder = base64.b64decode(zurueck.json()["content_base64"]).decode("utf-8")
    assert wieder == TEXT


def test_katalog_ueber_ext(client: TestClient) -> None:
    """Der Katalog haengt ebenfalls am zweiten Einhaengepunkt."""
    token = _lege_schluessel_an(label="Katalog")
    katalog = client.get("/api/ext/v1/entities", headers={"X-Api-Key": token})
    assert katalog.status_code == 200, katalog.text
    assert katalog.json()["entities"]


def test_begriffsliste_ueber_ext_trotz_fehlender_adminrolle(
    client_standard: TestClient,
) -> None:
    """Das Admin-Gate der Begriffsliste oeffnet allein wegen des Schluessels.

    Gemessen wird das an der Gegenprobe: derselbe Aufruf ohne Schluessel ueber
    den Menschenpfad muss mit 403 abgewiesen werden, weil der Benutzer hier
    ``standard`` und nicht ``admin`` ist. Erst diese beiden Haelften zusammen
    zeigen, dass ``has_api_key`` und nicht ``is_admin`` das Gate geoeffnet hat
    (Spec Abschnitt 4.4).
    """
    verweigert = client_standard.post(
        "/api/v1/terms", json={"term": "Ohne jedes Recht"}
    )
    assert verweigert.status_code == 403, verweigert.text

    headers = {"X-Api-Key": _lege_schluessel_an(label="Begriffe")}
    angelegt = client_standard.post(
        "/api/ext/v1/terms",
        json={"term": "Projekt Nordwind", "entity_type": "ORGANIZATION"},
        headers=headers,
    )
    assert angelegt.status_code == 201, angelegt.text
    try:
        liste = client_standard.get("/api/ext/v1/terms", headers=headers)
        assert liste.status_code == 200
        assert any(t["term"] == "Projekt Nordwind" for t in liste.json()["terms"])
    finally:
        weg = client_standard.delete(
            f"/api/ext/v1/terms/{angelegt.json()['id']}", headers=headers
        )
        assert weg.status_code == 200, weg.text


def test_vault_loeschen_ist_fuer_schluessel_gesperrt(
    client_standard: TestClient,
) -> None:
    """Die einzige Ausnahme von der Vollberechtigung des Schluessels.

    Ein Schluessel darf alles, was die Oberflaeche kann — ausser der einen
    unumkehrbaren Operation. Ein geleakter Schluessel soll nicht die
    Rueckfuehrbarkeit einer ganzen Organisation vernichten koennen; alles
    andere laesst sich nachtraeglich geraderuecken.

    Geprueft werden beide Haelften: die Abweisung **und** dass der Vault
    danach noch da ist. Ohne die zweite Haelfte bliebe der Test auch gruen,
    wenn geloescht und erst danach abgewiesen wuerde.
    """
    headers = {"X-Api-Key": _lege_schluessel_an(label="Loescher")}

    angelegt = client_standard.post(
        "/api/ext/v1/mappings", json={"label": "Wegwerf"}, headers=headers
    )
    assert angelegt.status_code == 201, angelegt.text
    vault_id = angelegt.json()["id"]

    verweigert = client_standard.delete(f"/api/v1/mappings/{vault_id}")
    assert verweigert.status_code == 403, verweigert.text

    ueber_schluessel = client_standard.delete(
        f"/api/ext/v1/mappings/{vault_id}", headers=headers
    )
    assert ueber_schluessel.status_code == 403, ueber_schluessel.text
    assert "API-Schluessel" in ueber_schluessel.json()["detail"]

    noch_da = client_standard.get(f"/api/ext/v1/mappings/{vault_id}", headers=headers)
    assert noch_da.status_code == 200, noch_da.text
    assert noch_da.json()["id"] == vault_id


def test_ext_und_v1_teilen_denselben_mandanten(client: TestClient) -> None:
    """Ein ueber die API angelegter Vault ist in der Oberflaeche sichtbar.

    Phase 1 kennt genau einen Mandanten; der Schluessel traegt ``tenant_id``
    bereits, damit Phase 2 ohne Schemaumbau trennen kann.
    """
    token = _lege_schluessel_an(label="Geteilt")
    angelegt = client.post(
        "/api/ext/v1/mappings",
        json={"label": "Ueber die API"},
        headers={"X-Api-Key": token},
    )
    assert angelegt.status_code == 201, angelegt.text

    ueber_gui = client.get("/api/v1/mappings")
    assert ueber_gui.status_code == 200
    assert any(m["id"] == angelegt.json()["id"] for m in ueber_gui.json()["mappings"])


# ── Aktor in der Nutzungserfassung ─────────────────────────────────────────


def test_nutzungseintrag_traegt_apikey_aktor(client: TestClient) -> None:
    """Der Aufrufer muss in ``usage_events`` erkennbar sein — nie mit Inhalten."""
    from sqlalchemy import select

    from backend.db import get_sessionmaker
    from backend.vault.models import UsageEvent

    token = _lege_schluessel_an(label="Aktor")
    r = client.post(
        "/api/ext/v1/documents:detect",
        json={"text": "Herr Andreas Meier"},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 200, r.text

    with get_sessionmaker()() as session:
        namen = set(session.execute(select(UsageEvent.username)).scalars().all())
    assert "apikey:Aktor" in namen


# ── Abgrenzung der beiden Pfade ────────────────────────────────────────────


def test_ext_pfad_verlangt_schluessel_auch_wenn_hub_auth_off(
    client: TestClient,
) -> None:
    """Dokumentiert die Absicht aus Spec Abschnitt 5.

    ``HUB_AUTH=off`` ist eine Erleichterung fuer den Menschenpfad. Griffe sie
    auch auf ``/api/ext/``, waere dieser Test lokal gruen und in Produktion
    rot — genau die Sorte Test, die nichts beweist.
    """
    r = client.post("/api/ext/v1/documents:detect", json={"text": "Hallo"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "API_KEY_MISSING"


def test_ext_pfad_wird_in_produktion_auf_https_umgeleitet(
    client: TestClient,
) -> None:
    """Der Maschinenpfad ist vom SSO-Gate ausgenommen, nicht vom HTTPS-Redirect.

    Genau deshalb hat ``main.py`` zwei Ausnahmelisten. Ein Klartext-Aufruf mit
    Schluessel im Header wird umgeleitet — der Token war dann zwar schon einmal
    unverschluesselt unterwegs, aber der Redirect ist der letzte Hinweis
    darauf, dass die Doku "nur https" ernst meint (Spec Abschnitt 5).

    308 und nicht 301: nur der erhaelt bei POST Methode und Body.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("ENV", "production")

    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        from backend.main import create_app

        with TestClient(create_app(), follow_redirects=False) as produktiv:
            r = produktiv.post(
                "/api/ext/v1/documents:detect",
                json={"text": "Hallo"},
                headers={"X-Api-Key": "ptps_egal"},
            )
            assert r.status_code == 308, r.text
            assert r.headers["location"].startswith("https://")

            # Gegenprobe: der Health-Endpunkt bleibt ausgenommen, sonst saehe
            # der Smoke-Test nach dem Deploy nur Redirects.
            assert produktiv.get("/api/health").status_code == 200
    finally:
        mp.undo()
        get_settings.cache_clear()


def test_openapi_weist_die_authentisierung_des_ext_pfads_aus(
    client: TestClient,
) -> None:
    """``/api/docs`` soll zeigen, dass /api/ext/v1 einen Schluessel verlangt.

    Ohne die Sicherheitsschemata an ``require_api_key`` saehe die Doku dort
    offene Endpunkte — und wer sie liest, probierte es ohne Header.
    """
    spec = client.get("/api/openapi.json").json()

    schemata = spec["components"]["securitySchemes"]
    assert any(
        eintrag.get("in") == "header" and eintrag.get("name") == "X-Api-Key"
        for eintrag in schemata.values()
    ), schemata

    ext = spec["paths"]["/api/ext/v1/documents:detect"]["post"]
    assert ext.get("security"), "der Maschinenpfad muss Authentisierung ausweisen"

    menschenpfad = spec["paths"]["/api/v1/documents:detect"]["post"]
    assert not menschenpfad.get("security"), (
        "der SSO-Pfad wird von der Middleware geschuetzt, nicht von einem Schema"
    )


def test_v1_pfad_bleibt_hinter_sso_gate(client: TestClient) -> None:
    """Mit eingeschaltetem Gate ist ``/api/v1`` ohne Cookie zu.

    Der zweite Einhaengepunkt darf keine Hintertuer in den Menschenpfad
    oeffnen. Geprueft wird beides an derselben App: ``/api/v1`` antwortet mit
    ``UNAUTHENTICATED`` aus der Middleware, ``/api/ext/v1`` mit
    ``API_KEY_MISSING`` aus der Router-Dependency — und in keinem Fall mit
    einem Redirect auf den Hub.

    Die ``client``-Fixture wird nur angefordert, damit Datenbank und
    Session-Factory schon stehen; benutzt wird sie hier nicht.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "on")

    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        from backend.main import create_app

        with TestClient(create_app(), follow_redirects=False) as gegated:
            menschenpfad = gegated.post(
                "/api/v1/documents:detect",
                json={"text": "Hallo"},
                headers={"accept": "application/json"},
            )
            assert menschenpfad.status_code == 401, menschenpfad.text
            assert menschenpfad.json()["code"] == "UNAUTHENTICATED"

            maschinenpfad = gegated.post(
                "/api/ext/v1/documents:detect", json={"text": "Hallo"}
            )
            assert maschinenpfad.status_code == 401, maschinenpfad.text
            assert maschinenpfad.json()["detail"]["code"] == "API_KEY_MISSING"

            # Und die Gegenprobe: ein gueltiger Schluessel oeffnet den
            # Menschenpfad NICHT. Sonst waere der zweite Einhaengepunkt eine
            # Umgehung des SSO-Gates statt einer Ergaenzung.
            mit_schluessel = gegated.post(
                "/api/v1/documents:detect",
                json={"text": "Hallo"},
                headers={
                    "X-Api-Key": _lege_schluessel_an(label="Kein Hintereingang"),
                    "accept": "application/json",
                },
            )
            assert mit_schluessel.status_code == 401, mit_schluessel.text
            assert mit_schluessel.json()["code"] == "UNAUTHENTICATED"

            # Ein Tippfehler im Maschinenpfad darf nicht in der SPA landen:
            # sonst gaebe die Ausnahme vom SSO-Gate die Seitenhuelle ohne
            # Anmeldung heraus — 200 mit HTML statt eines auswertbaren 404.
            daneben = gegated.get("/api/ext/v1/gibtsnicht")
            assert daneben.status_code == 404, daneben.text
            assert daneben.headers["content-type"].startswith("application/json")
    finally:
        mp.undo()
        get_settings.cache_clear()
