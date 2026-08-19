"""Verwaltung der API-Schluessel fuer ``/api/ext/v1`` (Design-Spec Abschnitt 4.5).

Warum nur ueber SSO erreichbar und **nicht** Teil von ``DUAL_MOUNT_ROUTERS``
(siehe ``main.py``): waere dieser Router auch unter ``/api/ext/v1`` eingehaengt,
koennte eine aufrufende Anwendung sich selbst weitere Schluessel ausstellen
oder alle Schluessel ihres Mandanten widerrufen — allein durch den Besitz
*eines* Schluessels. Das widerspraeche dem Zweck der Verwaltung: ein Mensch mit
CRM-Admin-Rolle stellt Schluessel aus und entzieht sie wieder, eine Anwendung
verwaltet nicht sich selbst. Alle drei Endpunkte pruefen deshalb ausschliesslich
``is_admin(request)`` — anders als ``terms.py``/``vaults.py``, wo ein
API-Schluessel als gleichwertig zur Admin-Rolle gilt.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.api_key_auth import generate_token, hash_token, key_prefix_of
from backend.core.deps import current_username, get_session, get_tenant
from backend.core.hub_access_auth import is_admin
from backend.vault.models import ApiKey, UsageEvent, as_utc

router = APIRouter(tags=["api-keys"])

#: Obergrenze der Bezeichnung. Historisch begruendet: solange der Aktor
#: ``apikey:<Bezeichnung>`` hiess, musste er in ``usage_events.username``
#: (``String(150)``) passen. Seit der Attribution ueber die Schluessel-ID ist
#: die Aktor-Laenge fest; die Grenze bleibt als harmlose Schranke fuer die
#: Anzeige bestehen.
LABEL_MAX_LENGTH = 140

#: Vorgabe des Auswertungsfensters in Tagen.
USAGE_DEFAULT_DAYS = 30

#: Obergrenze des Fensters (zehn Jahre). Groessere Werte sind kein sinnvolles
#: Fenster mehr, sondern ein Tippfehler — ``days=0`` ist der Weg zu "alles".
USAGE_MAX_DAYS = 3650

#: Praefix, das ``current_actor`` einem Maschinen-Aufruf voranstellt.
ACTOR_PREFIX = "apikey:"


class ApiKeyCreate(BaseModel):
    """Anfrage zum Erzeugen eines neuen Schluessels."""

    label: str = Field(..., min_length=1, max_length=LABEL_MAX_LENGTH)
    expires_at: Optional[datetime] = Field(
        None, description="Optionales Ablaufdatum. Muss in der Zukunft liegen."
    )

    @field_validator("label")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Die Bezeichnung darf nicht leer sein.")
        return v


def _require_admin(request: Request) -> None:
    """Gemeinsames Gate aller drei Endpunkte — siehe Modul-Docstring."""
    if not is_admin(request):
        raise HTTPException(
            403, detail="Nur Administratoren duerfen API-Schluessel verwalten."
        )


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, detail="Ungueltige Kennung.") from None


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Zeitstempel als ISO-8601 **mit** Zonenangabe, oder ``None``.

    Der Umweg ueber ``as_utc`` ist keine Kosmetik: unter SQLite kommen die
    Werte naiv aus der Datenbank zurueck, ``isoformat()`` schriebe sie dann
    ohne ``+00:00`` — und das Frontend liest einen ISO-String ohne Zone als
    **Lokalzeit**. Ein Schluessel saehe damit im Sommer zwei Stunden zu frueh
    erzeugt aus.
    """
    value = as_utc(value)
    return value.isoformat() if value is not None else None


def _to_dict(key: ApiKey) -> dict:
    """Antwortform ohne ``key_hash`` — der Hash verlaesst die Datenbank nie."""
    return {
        "id": str(key.id),
        "label": key.label,
        "key_prefix": key.key_prefix,
        "created_by": key.created_by,
        "created_at": _iso(key.created_at),
        "expires_at": _iso(key.expires_at),
        "last_used_at": _iso(key.last_used_at),
        "revoked_at": _iso(key.revoked_at),
        "active": key.is_active,
    }


@router.get("/api-keys")
async def list_api_keys(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> dict:
    """Alle Schluessel des Mandanten, neueste zuerst.

    Widerrufene bleiben sichtbar (Soft-Delete, siehe ``ApiKey``-Docstring) —
    nur ihr ``active`` wird ``false``.
    """
    _require_admin(request)
    tenant = get_tenant(session, request)
    rows = session.execute(
        select(ApiKey)
        .where(ApiKey.tenant_id == tenant.id)
        .order_by(ApiKey.created_at.desc())
    ).scalars()
    return {"api_keys": [_to_dict(r) for r in rows]}


def _usage_since(days: Optional[int]) -> tuple[Optional[int], Optional[datetime]]:
    """Prueft das Fenster und liefert ``(days, since)``.

    ``None`` und ``0`` bedeuten beide "alles" — ``0`` ist die Form, die sich
    ueber eine Query-Zeichenkette ausdruecken laesst. Alles ausserhalb von
    1..``USAGE_MAX_DAYS`` ist ein Bedienfehler und wird als 400 gemeldet, nicht
    stillschweigend zurechtgebogen: eine Auswertung, die ein anderes Fenster
    zeigt als das angefragte, ist schlimmer als eine Fehlermeldung.
    """
    if days is None or days == 0:
        return None, None
    if days < 1 or days > USAGE_MAX_DAYS:
        raise HTTPException(
            400,
            detail=(
                f"'days' muss zwischen 1 und {USAGE_MAX_DAYS} liegen "
                "(0 oder weglassen = gesamter Zeitraum)."
            ),
        )
    return days, datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/api-keys/usage")
async def api_key_usage(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[Optional[int], Query()] = USAGE_DEFAULT_DAYS,
) -> dict:
    """Nutzungsauswertung je Schluessel — Aufrufe, Aktionen, Kategorien, Zeichen.

    Beantwortet, was ``last_used_at`` nur halb beantwortet: nicht *ob* ein
    Schluessel noch benutzt wird, sondern **wie stark und wofuer**. Das ist die
    Grundlage fuer die Entscheidung, einen Schluessel zu widerrufen.

    Zugeordnet wird ueber die **Schluessel-ID**, nicht ueber die Bezeichnung
    (``current_actor`` schreibt ``apikey:<id>``, siehe ``core/deps.py``):
    Bezeichnungen sind nicht eindeutig, ein widerrufener Schluessel wird gern
    unter demselben Namen neu ausgestellt — nach Bezeichnung gruppiert bekaeme
    der neue die Aufrufe des alten zugeschrieben.

    Ausgewertet wird ausschliesslich ``usage_events``, und die Tabelle traegt
    per Konstruktion **nur Metadaten**: wer, was, wie viele, wann. Kein Text,
    kein Dateiname, kein erkannter Wert, kein Pseudonym (siehe
    ``UsageEvent``-Docstring). Diese Antwort kann deshalb nichts Inhaltliches
    verraten — auch nicht versehentlich.

    Aggregiert wird bewusst in Python und nicht in SQL: die Mengen sind klein
    (Schluessel je Mandant im einstelligen Bereich), die Summen ueber
    ``entity_counts`` waeren als JSON-Aggregat portabel zwischen PostgreSQL und
    SQLite ohnehin nicht zu haben — und Lesbarkeit schlaegt hier Laufzeit.

    Jeder Schluessel des Mandanten kommt vor, auch der widerrufene und der nie
    benutzte (dann mit Nullen). Sortiert nach Aufrufen absteigend, bei
    Gleichstand nach Erzeugungszeitpunkt absteigend.
    """
    _require_admin(request)
    fenster, since = _usage_since(days)
    tenant = get_tenant(session, request)

    keys = list(
        session.execute(
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant.id)
            .order_by(ApiKey.created_at.desc())
        ).scalars()
    )

    # Je Schluessel-ID ein Sammelbecken. ``defaultdict`` waere bequemer,
    # erzeugte aber Eintraege fuer Aktoren, deren Schluessel es nicht mehr
    # gibt — die gehoeren nicht in eine Liste, die nach Schluesseln geht.
    stat: dict[str, dict] = {
        str(k.id): {
            "requests": 0,
            "char_count": 0,
            "by_action": Counter(),
            "entity_counts": Counter(),
            "first_event_at": None,
            "last_event_at": None,
            "daily": defaultdict(lambda: {"requests": 0, "char_count": 0}),
        }
        for k in keys
    }

    stmt = select(UsageEvent).where(
        UsageEvent.tenant_id == tenant.id,
        UsageEvent.username.like(f"{ACTOR_PREFIX}%"),
    )
    if since is not None:
        stmt = stmt.where(UsageEvent.created_at >= since)

    for ereignis in session.execute(stmt).scalars():
        eintrag = stat.get(ereignis.username[len(ACTOR_PREFIX) :])
        if eintrag is None:
            # Schluessel geloescht (heute nur per Hand) — die Zahlen haben
            # keine Zeile mehr, in der sie stehen koennten.
            continue

        eintrag["requests"] += 1
        eintrag["char_count"] += ereignis.char_count or 0
        eintrag["by_action"][ereignis.action] += 1
        if isinstance(ereignis.entity_counts, dict):
            for typ, anzahl in ereignis.entity_counts.items():
                if isinstance(anzahl, int):
                    eintrag["entity_counts"][typ] += anzahl

        wann = as_utc(ereignis.created_at)
        if wann is not None:
            if eintrag["first_event_at"] is None or wann < eintrag["first_event_at"]:
                eintrag["first_event_at"] = wann
            if eintrag["last_event_at"] is None or wann > eintrag["last_event_at"]:
                eintrag["last_event_at"] = wann
            tag = eintrag["daily"][wann.date().isoformat()]
            tag["requests"] += 1
            tag["char_count"] += ereignis.char_count or 0

    zeilen = []
    for key in keys:
        s = stat[str(key.id)]
        zeilen.append(
            {
                "id": str(key.id),
                "label": key.label,
                "key_prefix": key.key_prefix,
                "active": key.is_active,
                "revoked_at": _iso(key.revoked_at),
                "expires_at": _iso(key.expires_at),
                "requests": s["requests"],
                "char_count": s["char_count"],
                "by_action": dict(s["by_action"].most_common()),
                "entity_counts": dict(s["entity_counts"].most_common()),
                "first_event_at": _iso(s["first_event_at"]),
                "last_event_at": _iso(s["last_event_at"]),
                # Nur Tage mit Ereignissen, aufsteigend. Luecken bleiben
                # Luecken: die Oberflaeche zeichnet Balken, keine Zeitachse.
                "daily": [
                    {"date": datum, **werte}
                    for datum, werte in sorted(s["daily"].items())
                ],
            }
        )

    # ``keys`` ist bereits nach ``created_at`` absteigend sortiert; ein
    # stabiler Sort nach Aufrufen erhaelt diese Reihenfolge bei Gleichstand.
    zeilen.sort(key=lambda z: z["requests"], reverse=True)

    return {
        "days": fenster,
        "since": _iso(since),
        "keys": zeilen,
    }


@router.post("/api-keys", status_code=201)
async def create_api_key(
    request: Request,
    payload: ApiKeyCreate,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Erzeugt einen neuen Schluessel.

    Der Klartext-Token steht nur in dieser Antwort — gespeichert wird
    ausschliesslich sein SHA-256 (siehe ``ApiKey``).
    """
    _require_admin(request)

    # Ein Ablaufdatum ohne Zeitzone gilt als UTC — dieselbe Regel, die
    # ``as_utc`` fuer bereits gespeicherte Werte anwendet.
    expires_at = as_utc(payload.expires_at)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise HTTPException(400, detail="Das Ablaufdatum muss in der Zukunft liegen.")

    tenant = get_tenant(session, request)
    token = generate_token()
    row = ApiKey(
        tenant_id=tenant.id,
        label=payload.label,
        key_prefix=key_prefix_of(token),
        key_hash=hash_token(token),
        created_by=current_username(request),
        expires_at=expires_at,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    antwort = _to_dict(row)
    antwort["token"] = token
    return antwort


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    request: Request, key_id: str, session: Annotated[Session, Depends(get_session)]
) -> dict:
    """Widerruft einen Schluessel (Soft-Delete).

    Idempotent: ein zweiter Aufruf auf denselben, bereits widerrufenen
    Schluessel bleibt 200.
    """
    _require_admin(request)
    tenant = get_tenant(session, request)
    row = session.get(ApiKey, _as_uuid(key_id))
    if row is None or row.tenant_id != tenant.id:
        raise HTTPException(404, detail="API-Schluessel nicht gefunden.")

    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return {"revoked": True}
