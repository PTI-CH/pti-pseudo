"""TIP-Hub SSO (Ansatz B) — Zugriffs-Gate fuer den PTI-Pseudonymisierer.

Portierung von ``pti-translator/backend/lib/hubAccessAuth.js``.

Das Tool haelt KEINE User-DB und KEIN Token-Secret. Es liest das geteilte
``refresh_token``-Cookie (Domain ``.pti-tip-hub.ch``, gesetzt vom CRM) und fragt
server-zu-server das CRM (``GET {CRM_BASE_URL}/api/auth/session``, mit
``X-Relay-Secret``), ob die Session gueltig ist UND ob der User das Recht fuer
dieses Tool hat. Das Ergebnis wird kurz gecacht (Single-Flight), damit ein
Seiten-Load mit vielen Assets nur EINE Introspection ausloest.

Nicht offensichtlich — bitte vor dem Anfassen lesen:

* **Dies ist der eigentliche Sicherheits-Check.** Ein Direktaufruf der Subdomain
  (Hub umgangen, URL direkt eingetippt) laeuft trotzdem hier durch. Die
  Hub-Kacheln sind nur Komfort, kein Schutz.
* **Middleware, nicht Route-Dependency.** Das statisch ausgelieferte SPA aus
  ``frontend/dist`` muss mitgegated sein; eine Dependency an den API-Routen
  wuerde das HTML ungeschuetzt lassen.
* **Fail closed.** Jede Exception und jede unerwartete CRM-Antwort gilt als
  "nicht authentisiert". Der HTTP-Client hat darum einen expliziten Timeout —
  ohne ihn wird aus "fail closed" ein "fail hang".
* ``user.person`` **kann None sein**; es gibt kein Top-Level-``email`` oder
  ``name``. Stabiler Schluessel ist ``username``, nicht die UUID.
* ``apps.<tool>`` und ``user.role`` sind **orthogonal**: ein Admin hat nicht
  automatisch Tool-Zugriff, und ein Tool-Berechtigter ist nicht automatisch
  Admin.
* Der Verdict-Cache liegt **pro gunicorn-Worker** (prozesslokales ``dict``).
  Bei 2 Workern also bis zu 2 Introspections pro Seitenload — akzeptiert.
* ``X-Relay-Secret`` ist ein Rate-Limit-Bypass beim CRM, **kein Auth-Faktor**.
"""

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote

import httpx
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from backend.config import Settings, get_settings

# Ohne expliziten Timeout wartet httpx unbegrenzt — aus "fail closed" wuerde ein
# "fail hang", und ein haengendes CRM legte das ganze Tool lahm.
INTROSPECTION_TIMEOUT_SECONDS = 5.0

# Positiv kurz cachen (ein Seiten-Load mit vielen Assets = eine Introspection),
# negativ noch kuerzer, damit ein frisch angemeldeter User nicht 30 s aussperrt.
POSITIVE_TTL_SECONDS = 30.0
NEGATIVE_TTL_SECONDS = 5.0

# Obergrenze fuer den Verdict-Cache. Der Schluessel ist der SHA-256 eines
# beliebigen (auch ungueltigen) Cookie-Werts — ein Angreifer kann mit jedem
# Request einen frischen, zufaelligen Token schicken und so beliebig viele
# Eintraege erzeugen, die nie wieder abgefragt und darum nie ueber die TTL
# bereinigt werden. Ohne Obergrenze waechst der Cache je Worker unbegrenzt und
# legt den Prozess irgendwann per Speicherverbrauch lahm. Der Wert ist
# grosszuegig bemessen: legitime Last (viele gleichzeitige Nutzer) bleibt weit
# darunter, nur ein Missbrauchsmuster stoesst je an die Grenze.
MAX_CACHE_ENTRIES = 10_000

# Phase 1: nur der Health-Endpunkt. Der Smoke-Test nach dem Deploy laeuft ohne
# Cookie und darf keinen Redirect sehen.
DEFAULT_EXEMPT_PATHS: tuple[re.Pattern[str], ...] = (re.compile(r"^/api/health$"),)

# Modulweiter Client: Connection-Pooling ueber alle Requests hinweg. Ein Client
# pro Introspection waere pro Seiten-Load ein neuer TLS-Handshake zum CRM.
_HTTP_CLIENT = httpx.AsyncClient(timeout=INTROSPECTION_TIMEOUT_SECONDS)

# Signatur der injizierbaren Introspection: (url, headers) -> Antwortobjekt mit
# ``status_code`` und ``json()``. Entspricht ``fetchImpl`` in der JS-Vorlage.
FetchImpl = Callable[[str, Mapping[str, str]], Awaitable[Any]]


async def _default_fetch(url: str, headers: Mapping[str, str]) -> httpx.Response:
    """Standard-Introspection ueber den modulweiten httpx-Client."""
    return await _HTTP_CLIENT.get(url, headers=dict(headers))


def read_refresh_token(cookie_header: str | None) -> str | None:
    """Liest genau den ``refresh_token``-Wert aus dem rohen Cookie-Header.

    Bewusst ohne Cookie-Parser: der Wert ist ein Token mit Base64url-Padding und
    kann darum ``=`` enthalten. Deshalb wird nur am ERSTEN ``=`` getrennt.

    Parameters
    ----------
    cookie_header : str or None
        Roher Inhalt des ``Cookie``-Headers.

    Returns
    -------
    str or None
        Der Token-Wert oder ``None``, wenn kein ``refresh_token`` enthalten ist.
    """
    if not cookie_header:
        return None
    for part in str(cookie_header).split(";"):
        index = part.find("=")
        if index == -1:
            continue
        if part[:index].strip() == "refresh_token":
            return part[index + 1 :].strip()
    return None


def wants_html(request: Request) -> bool:
    """Unterscheidet Top-Level-Navigation von XHR/fetch/Asset.

    Entscheidet Redirect (der Browser soll sich am Hub anmelden) gegen
    JSON-Fehler (die SPA soll eine auswertbare Antwort bekommen und nicht das
    HTML der Hub-Seite).
    """
    if request.headers.get("sec-fetch-mode") == "navigate":
        return True
    accept = request.headers.get("accept") or ""
    return request.method == "GET" and "text/html" in accept


def self_url(request: Request) -> str:
    """Rekonstruiert die vom Browser aufgerufene URL hinter dem Reverse-Proxy.

    ``request.url`` zeigt hinter dem Jelastic-LB auf den internen Host; fuer den
    ``?redirect=``-Parameter braucht der Hub aber die oeffentliche URL.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (forwarded_proto or request.url.scheme or "https").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    path = request.url.path or "/"
    query = request.url.query
    if query:
        path = f"{path}?{query}"
    return f"{scheme}://{host}{path}"


def is_admin(request: Request) -> bool:
    """Prueft, ob der angemeldete Hub-User Admin ist.

    Admin-Rechte kommen **ausschliesslich** aus ``role == "admin"``. Bewusst
    keine zusaetzliche Env-Allowlist: die liefe unweigerlich vom CRM auseinander
    und waere ein zweiter, unbeobachteter Rechte-Kanal.
    """
    hub_user = getattr(request.state, "hub_user", None)
    return isinstance(hub_user, Mapping) and hub_user.get("role") == "admin"


class HubAccessAuthMiddleware(BaseHTTPMiddleware):
    """Starlette-Middleware, die jeden Request gegen die CRM-Session prueft."""

    def __init__(
        self,
        app: Any,
        *,
        settings: Settings | None = None,
        exempt: Iterable[re.Pattern[str]] | None = None,
        fetch_impl: FetchImpl | None = None,
        now: Callable[[], float] | None = None,
        positive_ttl: float = POSITIVE_TTL_SECONDS,
        negative_ttl: float = NEGATIVE_TTL_SECONDS,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
    ) -> None:
        """Haengt das Gate vor die Anwendung.

        Parameters
        ----------
        app : Any
            Die nachgelagerte ASGI-Anwendung.
        settings : Settings, optional
            Konfiguration; Default ist ``get_settings()``.
        exempt : iterable of re.Pattern, optional
            Pfade, die ohne Auth durchgelassen werden. Default:
            ``^/api/health$``.
        fetch_impl : callable, optional
            Injizierbare Introspection (fuer Tests). Default: modulweiter
            httpx-Client.
        now : callable, optional
            Injizierbare Zeitquelle in Sekunden (fuer Tests). Default
            ``time.monotonic`` — monoton, damit ein NTP-Sprung den Cache nicht
            einfriert.
        positive_ttl : float
            Cache-Lebensdauer eines positiven Verdicts in Sekunden.
        negative_ttl : float
            Cache-Lebensdauer eines negativen Verdicts in Sekunden.
        max_cache_entries : int
            Obergrenze fuer den Verdict-Cache (siehe ``MAX_CACHE_ENTRIES``).
        """
        super().__init__(app)
        self._settings = settings if settings is not None else get_settings()
        self._exempt = tuple(exempt) if exempt is not None else DEFAULT_EXEMPT_PATHS
        self._fetch_impl = fetch_impl or _default_fetch
        self._now = now or time.monotonic
        self._positive_ttl = positive_ttl
        self._negative_ttl = negative_ttl
        self._max_cache_entries = max_cache_entries
        # key -> (expires_at, verdict). Schluessel ist der SHA-256 des Tokens,
        # damit der Rohtoken nicht als Dict-Schluessel im Speicher herumliegt.
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # key -> Future. Single-Flight: parallele Requests mit demselben Token
        # loesen genau EINE Introspection aus.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # ── Introspection ──────────────────────────────────────────────────────

    async def _introspect(self, token: str) -> dict[str, Any]:
        """Fragt das CRM nach der Session zum Token.

        Returns
        -------
        dict
            Bei Erfolg ``{"authenticated": True, "apps": ...,
            "two_factor_required": ..., "password_change_required": ...,
            "user": ...}``, sonst
            ``{"authenticated": False}``.
        """
        try:
            headers = {"Cookie": f"refresh_token={token}"}
            if self._settings.relay_secret:
                headers["X-Relay-Secret"] = self._settings.relay_secret
            url = f"{self._settings.crm_base_url}/api/auth/session"
            response = await self._fetch_impl(url, headers)
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    data = None
                if isinstance(data, Mapping) and data.get("authenticated"):
                    apps = data.get("apps") or {}
                    if not isinstance(apps, Mapping):
                        apps = {}
                    return {
                        "authenticated": True,
                        "apps": apps,
                        # Das CRM liefert camelCase; intern snake_case.
                        "two_factor_required": bool(data.get("twoFactorRequired")),
                        "password_change_required": bool(
                            data.get("passwordChangeRequired")
                        ),
                        "user": data.get("user") or None,
                    }
            return {"authenticated": False}
        except Exception:
            # Fail closed: CRM nicht erreichbar, Timeout, kaputtes JSON — alles
            # gilt als nicht authentisiert.
            return {"authenticated": False}

    async def _verdict_for(self, token: str) -> dict[str, Any]:
        """Liefert das Verdict aus Cache, laufendem Flug oder frischer Abfrage."""
        key = hashlib.sha256(token.encode()).hexdigest()

        cached = self._cache.get(key)
        if cached is not None and cached[0] > self._now():
            return cached[1]

        pending = self._pending.get(key)
        if pending is not None:
            # Zwischen dem Eintrag in _pending und dem set_result liegt fuer
            # Wartende kein await, darum ist der Zugriff hier atomar.
            return await pending

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[key] = future
        verdict: dict[str, Any] = {"authenticated": False}
        try:
            verdict = await self._introspect(token)
        finally:
            # Auch bei Cancellation aufloesen, sonst haengen Wartende ewig.
            if not future.done():
                future.set_result(verdict)
            self._pending.pop(key, None)

        ttl = self._positive_ttl if verdict.get("authenticated") else self._negative_ttl
        self._prune_cache()
        self._cache[key] = (self._now() + ttl, verdict)
        return verdict

    def _prune_cache(self) -> None:
        """Haelt den Verdict-Cache innerhalb von ``_max_cache_entries``.

        Zwei Schritte, in dieser Reihenfolge:

        1. Abgelaufene Eintraege raus — die faellt normalerweise beim naechsten
           Zugriff mit demselben Token an, aber ein Token, der nie ein zweites
           Mal vorkommt (Angreifer-Rauschen), wird sonst nie bereinigt.
        2. Reicht das nicht, die aeltesten verbleibenden Eintraege raus, bis
           wieder Platz fuer den neuen ist. ``dict`` in Python haelt die
           Einfuegereihenfolge, das macht das FIFO-Verhalten hier billig.

        Laeuft bei jedem neuen (nicht gecachten) Verdict — die Kosten sind ein
        einmaliger Scan ueber hoechstens ``_max_cache_entries`` Eintraege, das
        ist gegenueber der Introspection selbst vernachlaessigbar.
        """
        now = self._now()
        abgelaufen = [
            key for key, (expires_at, _) in self._cache.items() if expires_at <= now
        ]
        for key in abgelaufen:
            del self._cache[key]

        ueberschuss = len(self._cache) - self._max_cache_entries + 1
        if ueberschuss > 0:
            for key in list(self._cache.keys())[:ueberschuss]:
                del self._cache[key]

    # ── Antworten ──────────────────────────────────────────────────────────

    def _respond_unauthenticated(self, request: Request) -> Response:
        """Kein oder ungueltiges Token: zum Hub schicken bzw. 401."""
        if wants_html(request):
            redirect = quote(self_url(request), safe="")
            target = f"{self._settings.hub_base_url}/?redirect={redirect}"
            return RedirectResponse(target, status_code=302)
        return JSONResponse(
            {"code": "UNAUTHENTICATED", "message": "Bitte am TIP-Hub anmelden."},
            status_code=401,
        )

    def _respond_two_factor(self, request: Request) -> Response:
        """Session gueltig, aber 2FA im CRM offen: ins CRM schicken bzw. 403."""
        if wants_html(request):
            return RedirectResponse(f"{self._settings.crm_base_url}/", status_code=302)
        return JSONResponse(
            {
                "code": "TWO_FACTOR_REQUIRED",
                "message": "Zwei-Faktor-Authentisierung im CRM erforderlich.",
            },
            status_code=403,
        )

    def _respond_password_change(self, request: Request) -> Response:
        """Session gueltig, aber das Passwort muss zuerst gewechselt werden.

        Anders als bei 2FA fuehrt der Weg nicht auf die CRM-Wurzel, sondern
        direkt auf die Seite, auf der der Wechsel stattfindet — dort ist ohnehin
        nichts anderes zu tun.
        """
        if wants_html(request):
            target = f"{self._settings.crm_base_url}/security/change-password"
            return RedirectResponse(target, status_code=302)
        return JSONResponse(
            {
                "code": "FORCE_PASSWORD_CHANGE",
                "message": (
                    "Das Passwort muss geändert werden, "
                    "bevor fortgefahren werden kann."
                ),
            },
            status_code=403,
        )

    def _respond_forbidden(self, request: Request) -> Response:
        """Angemeldet, aber ohne Recht auf dieses Tool: zurueck auf den Hub."""
        if wants_html(request):
            return RedirectResponse(f"{self._settings.hub_base_url}/", status_code=302)
        return JSONResponse(
            {"code": "ACCESS_DENIED", "message": "Kein Zugriff auf dieses Tool."},
            status_code=403,
        )

    # ── Middleware ─────────────────────────────────────────────────────────

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Prueft den Request und laesst ihn durch oder weist ihn ab.

        Der ``try``-Block umschliesst **nur** die Pruefung, nicht den Aufruf der
        nachgelagerten Anwendung.

        Das ist wichtig und war frueher anders: lag ``call_next`` mit im
        ``try``, verwandelte das Gate **jeden** unbehandelten Fehler aus
        **jedem** Endpunkt in ein ``401 "Bitte am TIP-Hub anmelden."``. Ein
        echter Serverfehler sah damit aus wie ein Anmeldeproblem — er
        verschwand aus der Fehlerueberwachung, und wer ihn meldete, wurde zur
        Anmeldung geschickt statt zur Ursache. Nachgewiesen an einem zu langen
        Text, der die Erkennungs-Engine mit einem ``ValueError`` abbrechen
        liess und als 401 beim Aufrufer ankam.

        Fuer die Sicherheit aendert das nichts: faellt die Pruefung selbst aus,
        wird weiterhin fail closed abgewiesen. Nur was **nach** bestandener
        Pruefung passiert, darf jetzt ehrlich als 500 durchschlagen.
        """
        try:
            if self._settings.hub_auth == "off":
                # Dev ohne CRM: synthetischer User, damit Rollen-Gates und die
                # Nutzungserfassung lokal ueberhaupt jemanden sehen. Vorgabe
                # admin, damit die Admin-Seiten out of the box erreichbar sind;
                # DEV_ROLE=standard prueft den Verweigerungspfad.
                request.state.hub_user = {
                    "id": "dev",
                    "username": "dev",
                    "role": self._settings.dev_role or "admin",
                    "person": None,
                }
            elif any(pattern.search(request.url.path) for pattern in self._exempt):
                pass
            else:
                token = read_refresh_token(request.headers.get("cookie"))
                if not token:
                    return self._respond_unauthenticated(request)

                verdict = await self._verdict_for(token)
                if not verdict.get("authenticated"):
                    return self._respond_unauthenticated(request)
                # Reihenfolge wie im CRM (require2FACompliance,
                # pti-crm/backend/src/middleware/auth.ts): der erzwungene
                # Passwortwechsel wird VOR dem totpEnabled-Kurzschluss geprueft.
                # Wer beides offen hat, kann 2FA gar nicht einrichten, solange
                # das Passwort steht.
                if verdict.get("password_change_required"):
                    return self._respond_password_change(request)
                if verdict.get("two_factor_required"):
                    return self._respond_two_factor(request)
                if verdict.get("apps", {}).get(self._settings.hub_app_key) is not True:
                    return self._respond_forbidden(request)

                # Kann None sein — nachgelagerter Code darf sich nicht darauf
                # verlassen, dass hier ein Dict steht.
                request.state.hub_user = verdict.get("user")
        except Exception:
            # Unerwarteter Fehler in der Pruefung selbst: fail closed statt 500.
            return self._respond_unauthenticated(request)

        # Ausserhalb des try: ein Fehler der Anwendung ist ein Serverfehler und
        # soll auch als solcher sichtbar werden.
        return await call_next(request)


__all__ = [
    "DEFAULT_EXEMPT_PATHS",
    "HubAccessAuthMiddleware",
    "is_admin",
    "read_refresh_token",
    "self_url",
    "wants_html",
]
