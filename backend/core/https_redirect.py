"""HTTP-nach-HTTPS-Umleitung und Security-Header.

Portierung von ``pti-translator/backend/lib/security.js``.

Betrieb hinter dem Jelastic-NGINX-Loadbalancer: der reicht **Port 80
unverschluesselt durch** und setzt ``X-Forwarded-Proto`` **nur bei HTTPS**. Ein
fehlender Header bedeutet also nicht "lokal", sondern im Zweifel "Klartext".
Darum gilt eine Verbindung nur dann als sicher, wenn ``X-Forwarded-Proto``
ausdruecklich ``https`` sagt oder das Schema des Requests bereits ``https`` ist —
und in Produktion wird alles Uebrige umgeleitet.

``/api/health`` ist ausgenommen: der Deploy-Smoke-Test spricht den Knoten ueber
HTTP an und saehe sonst nur Redirects statt der eigentlichen Antwort.
"""

import re
from collections.abc import Iterable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from backend.config import Settings, get_settings

# Ausgenommen vom Redirect (nicht von den Security-Headern).
DEFAULT_EXEMPT_PATHS: tuple[re.Pattern[str], ...] = (re.compile(r"^/api/health$"),)

HSTS_VALUE = "max-age=31536000; includeSubDomains"


def forwarded_proto(request: Request) -> str:
    """Liest das erste Element aus ``X-Forwarded-Proto``, kleingeschrieben.

    Bei mehreren Proxies steht dort eine Kommaliste; massgeblich ist der
    aeusserste Eintrag, also der erste.
    """
    raw = request.headers.get("x-forwarded-proto") or ""
    return raw.split(",")[0].strip().lower()


def is_secure(request: Request) -> bool:
    """Sagt, ob die Verbindung nachweislich verschluesselt ist.

    Nachweislich heisst: ``X-Forwarded-Proto: https`` vom LB, oder der Request
    kam ohne Proxy direkt ueber TLS herein. Das blosse Fehlen des Headers zaehlt
    NICHT als sicher — genau das ist die Jelastic-Falle.
    """
    return forwarded_proto(request) == "https" or request.url.scheme == "https"


class HttpsRedirectMiddleware(BaseHTTPMiddleware):
    """Leitet in Produktion auf HTTPS um und setzt die Security-Header."""

    def __init__(
        self,
        app: Any,
        *,
        settings: Settings | None = None,
        exempt: Iterable[re.Pattern[str]] | None = None,
    ) -> None:
        """Haengt die Umleitung vor die Anwendung.

        Parameters
        ----------
        app : Any
            Die nachgelagerte ASGI-Anwendung.
        settings : Settings, optional
            Konfiguration; Default ist ``get_settings()``.
        exempt : iterable of re.Pattern, optional
            Pfade ohne Umleitung. Default: ``^/api/health$``.
        """
        super().__init__(app)
        self._settings = settings if settings is not None else get_settings()
        self._exempt = tuple(exempt) if exempt is not None else DEFAULT_EXEMPT_PATHS

    def _target_url(self, request: Request) -> str:
        """Baut das HTTPS-Ziel aus dem vom Client gesehenen Host und Pfad."""
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        )
        path = request.url.path or "/"
        query = request.url.query
        if query:
            path = f"{path}?{query}"
        return f"https://{host}{path}"

    def _apply_security_headers(self, response: Response, secure: bool) -> None:
        """Setzt die Security-Header auf jede Antwort, auch auf Redirects."""
        if secure:
            # HSTS nur ueber HTTPS setzen — ueber HTTP ignorieren Browser den
            # Header ohnehin, und ein falsch gesetzter waere schwer rueckgaengig
            # zu machen.
            response.headers["Strict-Transport-Security"] = HSTS_VALUE
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Leitet um oder laesst durch und haengt die Security-Header an."""
        secure = is_secure(request)
        exempt = any(pattern.search(request.url.path) for pattern in self._exempt)

        if not secure and self._settings.env == "production" and not exempt:
            # 308 erhaelt bei POST/PUT die Methode und den Body; 301 fuer
            # normale Seitenaufrufe, weil Browser und Proxies den besser cachen.
            status_code = 301 if request.method in ("GET", "HEAD") else 308
            response: Response = RedirectResponse(
                self._target_url(request), status_code=status_code
            )
            self._apply_security_headers(response, secure)
            return response

        response = await call_next(request)
        self._apply_security_headers(response, secure)
        return response


__all__ = [
    "DEFAULT_EXEMPT_PATHS",
    "HttpsRedirectMiddleware",
    "forwarded_proto",
    "is_secure",
]
