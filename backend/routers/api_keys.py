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
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.api_key_auth import generate_token, hash_token, key_prefix_of
from backend.core.deps import current_username, get_session, get_tenant
from backend.core.hub_access_auth import is_admin
from backend.vault.models import ApiKey, as_utc

router = APIRouter(tags=["api-keys"])

#: Siehe ``backend/core/deps.py``-Modulkopf: ``current_actor`` stellt
#: ``apikey:`` (7 Zeichen) voran, das Ergebnis landet in
#: ``usage_events.username`` — einer ``String(150)``. 140 + 7 = 147 <= 150.
LABEL_MAX_LENGTH = 140


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
