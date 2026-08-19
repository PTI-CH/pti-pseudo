"""Authentisierung des Maschinenpfads ``/api/ext/v1`` per API-Schluessel.

Gegenstueck zum SSO-Gate in ``hub_access_auth.py``: dort ein Mensch mit Cookie,
hier eine andere PTI-Anwendung mit einem Schluessel im Header. Vorbild ist der
``X-Api-Key`` des ``pti-translator``; abgewichen wird an einer Stelle bewusst —
die Schluessel liegen in der Datenbank statt in der Umgebung, weil hier mehrere
Anwendungen ohne Dashboard-Restart Zugang bekommen und verlieren sollen.

Nicht offensichtlich:

* **Dependency, nicht Middleware.** Anders als beim SSO-Gate gibt es hier keine
  statisch ausgelieferte Seite zu schuetzen — nur Routen. Der Einhaengepunkt
  ``/api/ext/v1`` traegt die Dependency als Router-Abhaengigkeit, und dieselben
  Handler bleiben unter ``/api/v1`` hinter dem SSO-Gate erreichbar.
* **Nie ein Redirect.** Ein Maschinenaufrufer kann sich nicht am Hub anmelden;
  er bekommt JSON mit einem Code, den er auswerten kann.
* **Der Token wird nie geloggt.** Im Fehlerfall hoechstens das Praefix — und
  auch das nur, wenn es einen Treffer gab.
* ``HUB_AUTH=off`` betrifft diesen Pfad **nicht**. Lokal ohne Schluessel zu
  arbeiten waere bequem, wuerde aber Tests gruen faerben, die in Produktion rot
  sind (Design-Spec Abschnitt 5).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.deps import get_session
from backend.vault.models import ApiKey, as_utc

logger = logging.getLogger(__name__)

#: Erkennungszeichen am Tokenanfang. Macht einen versehentlich in einem Log
#: oder Repository gelandeten Schluessel fuer Secret-Scanner auffindbar.
TOKEN_PREFIX = "ptps_"

#: Laenge des gespeicherten Wiedererkennungs-Praefixes (``ptps_`` + 7 Zeichen).
KEY_PREFIX_LENGTH = 12

#: Mindestabstand zwischen zwei Schreibvorgaengen auf ``last_used_at``. Ohne
#: diese Bremse waere jeder einzelne API-Aufruf zusaetzlich ein UPDATE auf
#: dieselbe Zeile — fachlich interessiert aber nur der Tag, nicht die Sekunde.
LAST_USED_WRITE_INTERVAL_SECONDS = 60

#: Header-Namen, in dieser Reihenfolge geprueft.
API_KEY_HEADER = "X-Api-Key"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "bearer "

# Die beiden Sicherheitsschemata sind vor allem **Beschreibung**: nur weil sie
# als Abhaengigkeit von ``require_api_key`` haengen, weist ``/api/docs`` fuer
# ``/api/ext/v1`` ueberhaupt eine Authentisierung aus — sonst saehe die Doku dort
# offene Endpunkte. ``auto_error=False`` bei beiden, weil nicht sie ueber den
# Fehler entscheiden sollen: die Reihenfolge (erst ``X-Api-Key``, dann Bearer)
# und die Unterscheidung MISSING/INVALID gehoeren in ``require_api_key``.
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


def generate_token() -> str:
    """Erzeugt einen neuen Klartext-Token.

    ``secrets.token_urlsafe(32)`` sind 32 zufaellige Bytes in 43 Zeichen. Diese
    Entropie ist der Grund, warum das Speichern eines einfachen SHA-256 genuegt:
    ein Woerterbuch- oder Brute-Force-Angriff auf den Hash ist aussichtslos, und
    ein langsames Verfahren wuerde nur jeden legitimen Aufruf verteuern.
    """
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 des Tokens als Hex — genau das, was in der Datenbank steht."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def key_prefix_of(token: str) -> str:
    """Die ersten Zeichen des Tokens zur Wiedererkennung in Liste und Log.

    Kurz genug, dass daraus nichts zu erraten ist, lang genug, um zwei
    Schluessel derselben Anwendung auseinanderzuhalten.
    """
    return token[:KEY_PREFIX_LENGTH]


def _bereinige(wert: str | None) -> str | None:
    """Leerraum weg; was danach leer ist, gilt als nicht gesetzt."""
    wert = (wert or "").strip()
    return wert or None


def read_api_key_header(
    request: Request,
    header_key: str | None = None,
    bearer_credentials: str | None = None,
) -> str | None:
    """Liest den Schluessel aus ``X-Api-Key`` oder ``Authorization: Bearer``.

    Beide Formen, weil beide vorkommen: ``X-Api-Key`` ist der Hausstandard der
    PTI-Werkzeuge, ``Bearer`` das, was generische HTTP-Clients und
    OpenAPI-Generatoren von sich aus schicken. ``X-Api-Key`` gewinnt, wenn
    beide da sind.

    ``header_key`` und ``bearer_credentials`` reicht ``require_api_key`` aus den
    Sicherheitsschemata durch — sie sind der Weg, ueber den die Authentisierung
    in ``/api/docs`` auftaucht. Fehlen sie (direkter Aufruf ohne
    FastAPI-Abhaengigkeiten), wird derselbe Wert aus den Rohheadern gelesen;
    das Ergebnis ist in beiden Faellen dasselbe.
    """
    treffer = _bereinige(header_key) or _bereinige(request.headers.get(API_KEY_HEADER))
    if treffer:
        return treffer

    treffer = _bereinige(bearer_credentials)
    if treffer:
        return treffer

    authorization = request.headers.get(AUTHORIZATION_HEADER) or ""
    if authorization.lower().startswith(BEARER_PREFIX):
        return _bereinige(authorization[len(BEARER_PREFIX) :])
    return None


def has_api_key(request: Request) -> bool:
    """Sagt, ob dieser Request ueber einen API-Schluessel authentisiert ist.

    Die Rechte-Gates der Router fragen damit, ob sie es mit einer aufrufenden
    Anwendung statt mit einem Menschen zu tun haben.
    """
    return getattr(request.state, "api_key", None) is not None


def _abweisen(code: str, message: str) -> HTTPException:
    """Baut die einheitliche 401-Antwort des Maschinenpfads.

    Bewusst ``detail`` als Objekt: FastAPI verpackt es zu
    ``{"detail": {"code": ..., "message": ...}}``, der Aufrufer kann also am
    Code entscheiden und muss keinen Text auswerten.
    """
    return HTTPException(status_code=401, detail={"code": code, "message": message})


def _touch_last_used(session: Session, api_key: ApiKey) -> None:
    """Vermerkt die Nutzung, hoechstens einmal pro Minute.

    Scheitert das Schreiben, wird es geloggt und verschluckt: ob der Zeitstempel
    aktuell ist, entscheidet nicht ueber die Berechtigung — ein deswegen
    abgebrochener Aufruf waere ein selbstgemachter Ausfall.
    """
    jetzt = datetime.now(timezone.utc)
    # SQLite liefert naive Zeitstempel zurueck; siehe ``models.as_utc``.
    zuletzt = as_utc(api_key.last_used_at)
    if zuletzt is not None:
        if (jetzt - zuletzt).total_seconds() < LAST_USED_WRITE_INTERVAL_SECONDS:
            return

    try:
        api_key.last_used_at = jetzt
        session.commit()
    except Exception:
        logger.warning(
            "last_used_at fuer Schluessel %s nicht geschrieben",
            api_key.key_prefix,
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:  # pragma: no cover — Rollback darf nie durchschlagen
            logger.debug("Rollback fehlgeschlagen", exc_info=True)


async def require_api_key(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    header_key: Annotated[str | None, Depends(api_key_scheme)] = None,
    bearer: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> ApiKey:
    """Router-Abhaengigkeit von ``/api/ext/v1``: gueltiger Schluessel oder 401.

    Der Lookup laeuft ueber den Unique-Index auf dem Hash — eine Zeile statt
    eines Tabellenscans. Der anschliessende ``hmac.compare_digest`` vergleicht
    zeitkonstant; er ist hier eher Sorgfalt als Notwendigkeit, weil bereits die
    Datenbank auf dem Hash und nicht auf dem Klartext sucht.

    ``header_key`` und ``bearer`` kommen aus den Sicherheitsschemata und stehen
    nur deshalb in der Signatur: sie tragen die Authentisierung in die
    OpenAPI-Beschreibung von ``/api/ext/v1``. Entschieden wird hier, nicht dort.

    Bei Erfolg steht der Schluessel unter ``request.state.api_key`` und die
    nachgelagerten Abhaengigkeiten (``get_tenant``, ``current_username``) lesen
    Mandant und Aktor von dort.

    Eine bewusst hingenommene Unschoenheit: FastAPI parst und validiert den
    Request-Body, **bevor** es die Abhaengigkeiten aufloest. Ein Aufruf mit
    kaputtem JSON und ganz ohne Schluessel bekommt deshalb 422 statt 401 — die
    Schluesselpruefung kommt gar nicht erst dazu. Das ist keine Umgehung (ohne
    Schluessel wird trotzdem nichts verarbeitet), sondern nur eine irrefuehrende
    Fehlernummer; sie zu begradigen hiesse, das Gate von einer Dependency zu
    einer Middleware umzubauen, und dafuer ist der Gewinn zu klein.
    """
    token = read_api_key_header(
        request,
        header_key=header_key,
        bearer_credentials=bearer.credentials if bearer is not None else None,
    )
    if not token:
        raise _abweisen(
            "API_KEY_MISSING",
            "Kein API-Schluessel. Header 'X-Api-Key' oder "
            "'Authorization: Bearer <token>' mitschicken.",
        )

    erwartet = hash_token(token)
    api_key = session.execute(
        select(ApiKey).where(ApiKey.key_hash == erwartet)
    ).scalar_one_or_none()

    if (
        api_key is None
        or not hmac.compare_digest(api_key.key_hash, erwartet)
        or not api_key.is_active
    ):
        # Ein einziger Code fuer unbekannt, widerrufen und abgelaufen: der
        # Aufrufer soll aus der Antwort nicht ablesen koennen, ob sein Token
        # jemals gueltig war.
        raise _abweisen(
            "API_KEY_INVALID",
            "API-Schluessel ungueltig, widerrufen oder abgelaufen.",
        )

    request.state.api_key = api_key
    _touch_last_used(session, api_key)
    return api_key


__all__ = [
    "API_KEY_HEADER",
    "LAST_USED_WRITE_INTERVAL_SECONDS",
    "TOKEN_PREFIX",
    "generate_token",
    "has_api_key",
    "hash_token",
    "key_prefix_of",
    "read_api_key_header",
    "require_api_key",
]
