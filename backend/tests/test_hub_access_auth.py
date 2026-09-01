"""Tests fuer das TIP-Hub-SSO-Gate.

Das CRM wird nie wirklich angesprochen: die Introspection ist ueber den
``fetch_impl``-Parameter der Middleware injizierbar, die Zeitquelle ueber
``now`` — genau wie ``fetchImpl``/``now`` in der JS-Vorlage.

Alle Tests sind synchron. Wo echte Nebenlaeufigkeit noetig ist (Single-Flight),
wird ``asyncio.run`` mit ``httpx.ASGITransport`` verwendet, damit die Suite ohne
``asyncio_mode``-Konfiguration in einer pytest.ini/pyproject auskommt.
"""

import asyncio
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.core.hub_access_auth import (
    HubAccessAuthMiddleware,
    is_admin,
    read_refresh_token,
)
from backend.routers import health

TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload=="
COOKIE = f"refresh_token={TOKEN}"

# Kopf eines Browsers bei Top-Level-Navigation.
HTML_HEADERS = {"accept": "text/html,application/xhtml+xml", "cookie": COOKIE}
# Kopf eines fetch()-Aufrufs der SPA.
XHR_HEADERS = {"accept": "application/json", "cookie": COOKIE}

SESSION_OK: dict[str, Any] = {
    "authenticated": True,
    "apps": {"pseudonymizer": True, "translator": False},
    "twoFactorRequired": False,
    # user.person darf None sein — das Gate darf sich nicht darauf verlassen.
    "user": {"id": "u1", "username": "muster", "role": "standard", "person": None},
}


# ── Hilfsmittel ────────────────────────────────────────────────────────────


class FakeResponse:
    """Minimale Nachbildung einer httpx-Antwort."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        """Liefert den Payload; ein Exception-Payload wird geworfen."""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class RecordingFetch:
    """Zaehlt Introspections und merkt sich URL und Header des letzten Aufrufs."""

    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else SESSION_OK
        self.calls = 0
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def __call__(self, url: str, headers: Any) -> FakeResponse:
        """Beantwortet die Introspection ohne Netzwerk."""
        self.calls += 1
        self.urls.append(url)
        self.headers.append(dict(headers))
        return FakeResponse(self.status_code, self.payload)


class RaisingFetch:
    """Introspection, die immer scheitert (Timeout, DNS, TLS)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or httpx.ConnectTimeout("CRM antwortet nicht")
        self.calls = 0

    async def __call__(self, url: str, headers: Any) -> FakeResponse:
        """Wirft den konfigurierten Fehler."""
        self.calls += 1
        raise self.error


class Clock:
    """Injizierbare, von Hand vorstellbare Zeitquelle."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        """Liefert die aktuelle kuenstliche Zeit in Sekunden."""
        return self.value

    def advance(self, seconds: float) -> None:
        """Stellt die Uhr vor."""
        self.value += seconds


def make_settings(**overrides: Any) -> Settings:
    """Baut Settings, die weder Prozessumgebung noch .env-Datei lesen."""
    values: dict[str, Any] = {
        "_env_file": None,
        "env": "production",
        "crm_base_url": "https://crm.example.ch/",
        "hub_base_url": "https://hub.example.ch/",
        "hub_app_key": "pseudonymizer",
        "relay_secret": "relay-geheim",
        "public_base_url": "https://pseudonymizer.example.ch",
        "hub_auth": None,
        "dev_role": "",
    }
    values.update(overrides)
    return Settings(**values)


def build_app(**middleware_options: Any) -> FastAPI:
    """Mini-App mit Health-Router, einer Seite und einer schreibenden Route."""
    app = FastAPI()
    app.include_router(health.router)

    @app.get("/")
    async def index(request: Request) -> dict[str, Any]:
        return {
            "seite": "index",
            "hub_user": getattr(request.state, "hub_user", None),
            "admin": is_admin(request),
        }

    @app.get("/dokumente")
    async def dokumente(request: Request) -> dict[str, Any]:
        return {"seite": "dokumente"}

    app.add_middleware(HubAccessAuthMiddleware, **middleware_options)
    return app


def client_for(**middleware_options: Any) -> TestClient:
    """TestClient ohne automatisches Folgen von Redirects."""
    middleware_options.setdefault("settings", make_settings())
    return TestClient(build_app(**middleware_options), follow_redirects=False)


# ── 1. Erfolgsfall ─────────────────────────────────────────────────────────


def test_authentifiziert_mit_flag_laesst_durch() -> None:
    """CRM meldet authentifiziert und apps.pseudonymizer == True."""
    fetch = RecordingFetch()
    client = client_for(fetch_impl=fetch)

    response = client.get("/", headers=HTML_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["seite"] == "index"
    assert body["hub_user"]["username"] == "muster"
    # apps und role sind orthogonal: Tool-Zugriff macht niemanden zum Admin.
    assert body["admin"] is False
    assert fetch.calls == 1
    assert fetch.urls[0] == "https://crm.example.ch/api/auth/session"
    assert fetch.headers[0]["Cookie"] == COOKIE
    assert fetch.headers[0]["X-Relay-Secret"] == "relay-geheim"


def test_relay_secret_wird_ohne_wert_nicht_gesendet() -> None:
    """Leeres RELAY_SECRET darf keinen leeren Header erzeugen."""
    fetch = RecordingFetch()
    client = client_for(settings=make_settings(relay_secret=""), fetch_impl=fetch)

    assert client.get("/", headers=HTML_HEADERS).status_code == 200
    assert "X-Relay-Secret" not in fetch.headers[0]


def test_rolle_admin_wird_erkannt() -> None:
    """is_admin liest ausschliesslich role == 'admin'."""
    payload = {
        **SESSION_OK,
        "user": {"id": "u2", "username": "chef", "role": "admin", "person": None},
    }
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    assert client.get("/", headers=HTML_HEADERS).json()["admin"] is True


# ── 2./3. Nicht authentisiert ──────────────────────────────────────────────


def test_nicht_authentisiert_html_redirect_auf_hub() -> None:
    """Browser-Navigation ohne gueltige Session wird zum Hub umgeleitet."""
    payload = {"authenticated": False}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    response = client.get(
        "/dokumente?id=7",
        headers={
            **HTML_HEADERS,
            "x-forwarded-proto": "https",
            "x-forwarded-host": "pseudonymizer.example.ch",
        },
    )

    assert response.status_code == 302
    erwartet = quote("https://pseudonymizer.example.ch/dokumente?id=7", safe="")
    assert response.headers["location"] == f"https://hub.example.ch/?redirect={erwartet}"


def test_sec_fetch_mode_navigate_gilt_als_html() -> None:
    """Auch ohne Accept-Header ist eine Navigation eine Navigation."""
    client = client_for(fetch_impl=RecordingFetch(payload={"authenticated": False}))

    response = client.get(
        "/", headers={"cookie": COOKIE, "accept": "*/*", "sec-fetch-mode": "navigate"}
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://hub.example.ch/?redirect=")


def test_nicht_authentisiert_xhr_liefert_401() -> None:
    """Die SPA bekommt JSON, damit sie den Zustand auswerten kann."""
    client = client_for(fetch_impl=RecordingFetch(payload={"authenticated": False}))

    response = client.get("/", headers=XHR_HEADERS)

    assert response.status_code == 401
    assert response.json() == {
        "code": "UNAUTHENTICATED",
        "message": "Bitte am TIP-Hub anmelden.",
    }


def test_ohne_cookie_wird_gar_nicht_erst_gefragt() -> None:
    """Ohne Token entfaellt die Introspection."""
    fetch = RecordingFetch()
    client = client_for(fetch_impl=fetch)

    response = client.get("/", headers={"accept": "application/json"})

    assert response.status_code == 401
    assert fetch.calls == 0


# ── 4. Fail closed ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("headers", "erwarteter_status"),
    [(HTML_HEADERS, 302), (XHR_HEADERS, 401)],
)
def test_crm_timeout_faellt_geschlossen_aus(
    headers: dict[str, str], erwarteter_status: int
) -> None:
    """CRM nicht erreichbar heisst nicht authentisiert — und niemals 500."""
    client = client_for(fetch_impl=RaisingFetch())

    response = client.get("/", headers=headers)

    assert response.status_code == erwarteter_status


def test_kaputtes_json_faellt_geschlossen_aus() -> None:
    """200 mit unlesbarem Body ist keine gueltige Session."""
    fetch = RecordingFetch(payload=ValueError("kein JSON"))
    client = client_for(fetch_impl=fetch)

    assert client.get("/", headers=XHR_HEADERS).status_code == 401


def test_status_500_vom_crm_faellt_geschlossen_aus() -> None:
    """Nur 200 mit authenticated == True zaehlt."""
    fetch = RecordingFetch(status_code=500, payload=SESSION_OK)
    client = client_for(fetch_impl=fetch)

    assert client.get("/", headers=XHR_HEADERS).status_code == 401


# ── 5. Zwei-Faktor ─────────────────────────────────────────────────────────


def test_zwei_faktor_html_redirect_ins_crm() -> None:
    """Offene 2FA schickt den Browser ins CRM, nicht auf den Hub."""
    payload = {**SESSION_OK, "twoFactorRequired": True}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    response = client.get("/", headers=HTML_HEADERS)

    assert response.status_code == 302
    assert response.headers["location"] == "https://crm.example.ch/"


def test_zwei_faktor_xhr_liefert_403() -> None:
    """Offene 2FA als JSON-Fehler fuer die SPA."""
    payload = {**SESSION_OK, "twoFactorRequired": True}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    response = client.get("/", headers=XHR_HEADERS)

    assert response.status_code == 403
    assert response.json() == {
        "code": "TWO_FACTOR_REQUIRED",
        "message": "Zwei-Faktor-Authentisierung im CRM erforderlich.",
    }


# ── 5b. Erzwungener Passwortwechsel ────────────────────────────────────────


def test_passwortwechsel_html_redirect_auf_die_crm_passwortseite() -> None:
    """Offener Passwortwechsel schickt den Browser auf die Wechselseite.

    Anders als bei 2FA reicht die CRM-Wurzel hier nicht: dort ist nichts zu
    tun, ausser das Passwort zu aendern.
    """
    payload = {**SESSION_OK, "passwordChangeRequired": True}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    response = client.get("/", headers=HTML_HEADERS)

    assert response.status_code == 302
    assert (
        response.headers["location"]
        == "https://crm.example.ch/security/change-password"
    )


def test_passwortwechsel_xhr_liefert_403() -> None:
    """Offener Passwortwechsel als JSON-Fehler fuer die SPA.

    Das CRM sperrt solche Konten auf jeder Business-Route mit
    ``403 FORCE_PASSWORD_CHANGE`` aus. Ohne diese Pruefung endete die Sperre an
    der SSO-Grenze: wer die Adresse dieses Werkzeugs direkt eintippt, kam trotz
    offenem Passwortwechsel herein.
    """
    payload = {**SESSION_OK, "passwordChangeRequired": True}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    response = client.get("/", headers=XHR_HEADERS)

    assert response.status_code == 403
    assert response.json() == {
        "code": "FORCE_PASSWORD_CHANGE",
        "message": "Das Passwort muss geändert werden, bevor fortgefahren werden kann.",
    }


def test_passwortwechsel_sticht_die_zwei_faktor_pflicht() -> None:
    """Reihenfolge wie im CRM: das Passwort kommt vor der 2FA-Einrichtung.

    ``require2FACompliance`` (pti-crm/backend/src/middleware/auth.ts) prueft
    ``mustChangePassword`` ausdruecklich VOR dem ``totpEnabled``-Kurzschluss —
    wer beides offen hat, kann 2FA gar nicht einrichten, solange das Passwort
    steht.
    """
    payload = {**SESSION_OK, "twoFactorRequired": True, "passwordChangeRequired": True}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    response = client.get("/", headers=XHR_HEADERS)

    assert response.json()["code"] == "FORCE_PASSWORD_CHANGE"


# ── 6. Kein Tool-Recht ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "apps",
    [
        {},
        {"pseudonymizer": False},
        {"translator": True},
        # Truthy, aber nicht True: der Vergleich ist bewusst identitaetsstreng.
        {"pseudonymizer": "true"},
        {"pseudonymizer": 1},
    ],
    ids=["leer", "false", "anderes-tool", "string", "eins"],
)
def test_fehlendes_app_flag_verweigert(apps: dict[str, Any]) -> None:
    """Ohne apps.pseudonymizer == True gibt es keinen Zugriff."""
    payload = {**SESSION_OK, "apps": apps}
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    html = client.get("/", headers=HTML_HEADERS)
    assert html.status_code == 302
    assert html.headers["location"] == "https://hub.example.ch/"

    xhr = client.get("/", headers=XHR_HEADERS)
    assert xhr.status_code == 403
    assert xhr.json() == {
        "code": "ACCESS_DENIED",
        "message": "Kein Zugriff auf dieses Tool.",
    }


def test_admin_ohne_tool_flag_bleibt_draussen() -> None:
    """role == admin ersetzt das Tool-Flag nicht (Orthogonalitaet)."""
    payload = {
        "authenticated": True,
        "apps": {},
        "twoFactorRequired": False,
        "user": {"id": "u2", "username": "chef", "role": "admin", "person": None},
    }
    client = client_for(fetch_impl=RecordingFetch(payload=payload))

    assert client.get("/", headers=XHR_HEADERS).status_code == 403


# ── 7. Single-Flight ───────────────────────────────────────────────────────


class GatedFetch:
    """Introspection, die sich anhalten laesst — fuer den Single-Flight-Test."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, url: str, headers: Any) -> FakeResponse:
        """Meldet den Start und wartet, bis der Test freigibt."""
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return FakeResponse(200, self.payload)


def test_single_flight_buendelt_parallele_requests() -> None:
    """Zwei gleichzeitige Requests mit demselben Token = EINE Introspection.

    Der Cache ist hier mit ``positive_ttl=0.0`` abgeschaltet, damit wirklich das
    Single-Flight geprueft wird und nicht versehentlich der Cache-Treffer.
    """

    async def szenario() -> tuple[int, list[int]]:
        gate = GatedFetch(SESSION_OK)
        app = build_app(
            settings=make_settings(),
            fetch_impl=gate,
            positive_ttl=0.0,
            negative_ttl=0.0,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            erster = asyncio.create_task(client.get("/", headers=XHR_HEADERS))
            # Warten, bis der erste Request in der Introspection steht.
            await gate.started.wait()
            zweiter = asyncio.create_task(client.get("/", headers=XHR_HEADERS))
            # Dem zweiten Request Zeit geben, das Gate zu erreichen und sich am
            # Future des ersten anzustellen.
            await asyncio.sleep(0.05)
            gate.release.set()
            antworten = await asyncio.gather(erster, zweiter)
        return gate.calls, [antwort.status_code for antwort in antworten]

    calls, status_codes = asyncio.run(szenario())

    assert calls == 1
    assert status_codes == [200, 200]


# ── 8. Cache ───────────────────────────────────────────────────────────────


def test_cache_trifft_innerhalb_der_positiv_ttl() -> None:
    """Zweiter Request in der TTL fragt das CRM nicht erneut."""
    fetch = RecordingFetch()
    clock = Clock()
    client = client_for(fetch_impl=fetch, now=clock)

    assert client.get("/", headers=XHR_HEADERS).status_code == 200
    assert fetch.calls == 1

    clock.advance(29.0)
    assert client.get("/", headers=XHR_HEADERS).status_code == 200
    assert fetch.calls == 1

    # Nach Ablauf der 30 s wird neu gefragt.
    clock.advance(2.0)
    assert client.get("/", headers=XHR_HEADERS).status_code == 200
    assert fetch.calls == 2


def test_negative_ttl_ist_kuerzer() -> None:
    """Ein negatives Verdict verfaellt nach 5 s, nicht erst nach 30 s."""
    fetch = RecordingFetch(payload={"authenticated": False})
    clock = Clock()
    client = client_for(fetch_impl=fetch, now=clock)

    assert client.get("/", headers=XHR_HEADERS).status_code == 401
    assert fetch.calls == 1

    clock.advance(6.0)
    assert client.get("/", headers=XHR_HEADERS).status_code == 401
    assert fetch.calls == 2


def test_verschiedene_tokens_teilen_den_cache_nicht() -> None:
    """Der Cache-Schluessel ist der SHA-256 des jeweiligen Tokens."""
    fetch = RecordingFetch()
    client = client_for(fetch_impl=fetch, now=Clock())

    client.get("/", headers={"accept": "application/json", "cookie": COOKIE})
    client.get("/", headers={"accept": "application/json", "cookie": "refresh_token=b"})

    assert fetch.calls == 2


def test_cache_waechst_nicht_unbegrenzt_bei_vielen_verschiedenen_tokens() -> None:
    """Beliebig viele zufaellige Cookies duerfen den Cache nicht aufblaehen.

    Jeder Request mit einem NEUEN ``refresh_token``-Cookie legt (mangels
    Cache-Treffer) einen neuen Eintrag im prozesslokalen Verdict-Cache an.
    Ohne Obergrenze wird dieser Eintrag nie wieder entfernt, wenn derselbe
    Token nie ein zweites Mal vorkommt (typisch fuer zufaellig geratene
    Cookies) — der Cache waechst dann mit jeder Angreifer-Anfrage weiter und
    legt den Worker-Prozess irgendwann per Speicherverbrauch lahm.
    """
    fetch = RecordingFetch(payload={"authenticated": False})
    middleware = HubAccessAuthMiddleware(
        FastAPI(),
        settings=make_settings(),
        fetch_impl=fetch,
        now=Clock(),
        max_cache_entries=50,
    )

    async def szenario() -> int:
        for i in range(500):
            await middleware._verdict_for(f"angreifer-token-{i}")
        return len(middleware._cache)

    cache_groesse = asyncio.run(szenario())

    assert cache_groesse <= 50


# ── 9. Cookie-Parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cookie_header", "erwartet"),
    [
        ("refresh_token=abc", "abc"),
        # Der Wert darf '=' enthalten: nur am ERSTEN '=' trennen.
        ("refresh_token=a.b.c==", "a.b.c=="),
        ("session=1; refresh_token=abc; theme=dark", "abc"),
        # Leerzeichen um Name und Wert werden getrimmt.
        ("  session=1 ;   refresh_token =  abc  ; x=2", "abc"),
        ("refresh_token=", ""),
        ("session=1; theme=dark", None),
        # Ein Segment ohne '=' darf das Parsen nicht abbrechen.
        ("kaputt; refresh_token=abc", "abc"),
        ("", None),
        (None, None),
        # Kein Praefix-Treffer: refresh_token_alt ist nicht refresh_token.
        ("refresh_token_alt=xyz", None),
    ],
    ids=[
        "einfach",
        "gleichheitszeichen-im-wert",
        "mehrere-cookies",
        "leerzeichen",
        "leerer-wert",
        "nicht-vorhanden",
        "segment-ohne-gleich",
        "leerer-header",
        "kein-header",
        "aehnlicher-name",
    ],
)
def test_read_refresh_token(cookie_header: str | None, erwartet: str | None) -> None:
    """Rohes Cookie-Parsing ohne Cookie-Bibliothek."""
    assert read_refresh_token(cookie_header) == erwartet


def test_token_mit_gleichheitszeichen_erreicht_das_crm_unveraendert() -> None:
    """Ende-zu-Ende: das Padding des Tokens darf nicht verloren gehen."""
    fetch = RecordingFetch()
    client = client_for(fetch_impl=fetch)

    response = client.get(
        "/",
        headers={
            "accept": "application/json",
            "cookie": f"theme=dark; {COOKIE}; session=1",
        },
    )

    assert response.status_code == 200
    assert fetch.headers[0]["Cookie"] == f"refresh_token={TOKEN}"


# ── 10. Dev-Bypass ─────────────────────────────────────────────────────────


def test_hub_auth_off_laesst_mit_synthetischem_admin_durch() -> None:
    """HUB_AUTH=off: kein CRM, synthetischer Admin."""
    fetch = RecordingFetch()
    client = client_for(settings=make_settings(hub_auth="off"), fetch_impl=fetch)

    response = client.get("/", headers={"accept": "application/json"})

    assert response.status_code == 200
    assert response.json()["hub_user"] == {
        "id": "dev",
        "username": "dev",
        "role": "admin",
        "person": None,
    }
    assert response.json()["admin"] is True
    assert fetch.calls == 0


def test_dev_role_standard_prueft_den_verweigerungspfad() -> None:
    """DEV_ROLE=standard erzeugt einen Nicht-Admin."""
    settings = make_settings(hub_auth="off", dev_role="standard")
    client = client_for(settings=settings, fetch_impl=RecordingFetch())

    body = client.get("/", headers={"accept": "application/json"}).json()

    assert body["hub_user"]["role"] == "standard"
    assert body["admin"] is False


def test_hub_auth_anderer_wert_schaltet_nicht_ab() -> None:
    """Nur exakt 'off' deaktiviert das Gate — 'false' oder 'on' nicht."""
    client = client_for(settings=make_settings(hub_auth="false"))

    assert client.get("/", headers={"accept": "application/json"}).status_code == 401


# ── 11. Ausgenommene Pfade ─────────────────────────────────────────────────


def test_health_ist_ohne_cookie_erreichbar() -> None:
    """Der Smoke-Test nach dem Deploy laeuft ohne Session."""
    fetch = RecordingFetch()
    client = client_for(fetch_impl=fetch)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "pti-pseudonymisierer"
    assert isinstance(body["version"], str)
    assert fetch.calls == 0


def test_nur_health_ist_ausgenommen() -> None:
    """Aehnliche Pfade duerfen nicht mit ausgenommen werden.

    Die Regex ist auf ``^/api/health$`` verankert; ein unverankertes Muster
    liesse ``/api/healthz`` und ``/api/health/geheim`` ungeschuetzt durch.
    """
    client = client_for(fetch_impl=RecordingFetch(payload={"authenticated": False}))

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/healthz", headers=XHR_HEADERS).status_code == 401
    assert client.get("/api/health/geheim", headers=XHR_HEADERS).status_code == 401


# ── Fehler der Anwendung duerfen nicht als Anmeldeproblem getarnt werden ────


def test_fehler_im_endpunkt_wird_nicht_als_401_getarnt():
    """Ein Absturz IM Endpunkt muss als Serverfehler durchschlagen.

    Frueher umschloss der try-Block auch ``call_next``. Jeder unbehandelte
    Fehler eines Endpunkts wurde dadurch zu einem ``401 UNAUTHENTICATED`` —
    ein echter Serverfehler sah aus wie ein Anmeldeproblem, verschwand aus der
    Fehlerueberwachung, und wer ihn meldete, wurde zur Anmeldung geschickt.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.core.hub_access_auth import HubAccessAuthMiddleware

    app = FastAPI()

    @app.get("/api/kaputt")
    async def _kaputt() -> dict:
        raise RuntimeError("absichtlicher Fehler im Endpunkt")

    app.add_middleware(
        HubAccessAuthMiddleware,
        settings=make_settings(hub_auth="off"),
    )

    client = TestClient(app, raise_server_exceptions=False)
    antwort = client.get("/api/kaputt")

    assert antwort.status_code == 500, (
        "Ein Fehler im Endpunkt muss als 500 sichtbar werden, nicht als "
        f"Anmeldeproblem — bekommen: {antwort.status_code} {antwort.text[:120]}"
    )
    assert "UNAUTHENTICATED" not in antwort.text


def test_fehler_in_der_pruefung_faellt_weiterhin_geschlossen_aus():
    """Die Gegenprobe: ein Fehler im Gate selbst weist weiterhin ab.

    Ohne diesen Test koennte man den obigen erfuellen, indem man das
    Fail-closed-Verhalten mit abschafft — und genau das darf nicht passieren.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.core.hub_access_auth import HubAccessAuthMiddleware

    app = FastAPI()

    @app.get("/api/v1/geschuetzt")
    async def _ok() -> dict:
        return {"ok": True}

    async def _kaputte_introspection(url, headers):
        raise RuntimeError("CRM unerreichbar")

    app.add_middleware(
        HubAccessAuthMiddleware,
        settings=make_settings(),
        fetch_impl=_kaputte_introspection,
    )

    client = TestClient(app, raise_server_exceptions=False)
    antwort = client.get(
        "/api/v1/geschuetzt", headers={"cookie": "refresh_token=irgendwas"}
    )

    assert antwort.status_code == 401, (
        "Faellt die Pruefung selbst aus, muss weiterhin abgewiesen werden — "
        f"bekommen: {antwort.status_code}"
    )
    assert "UNAUTHENTICATED" in antwort.text
