"""Verwaltung der Zuordnungstabellen (Vaults).

Ein Vault haelt die Zuordnung Original→Pseudonym eines oder mehrerer Dokumente.
Er ist der sensibelste Bestand des Systems: die Originale sind AES-GCM
verschluesselt, aber wer den Vault loeschen darf, entscheidet ueber die
Rueckfuehrbarkeit.

Bewusst NICHT ausgeliefert: der entschluesselte Originaltext einzelner Eintraege.
Die Liste zeigt Typ, Pseudonym und Zeitpunkt — wer den Klartext braucht, fuehrt
ein Dokument zurueck. So gibt es keinen Endpunkt, der den Vault ausleert.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.api_key_auth import has_api_key
from backend.core.deps import get_session, get_tenant
from backend.core.hub_access_auth import is_admin
from backend.engine import entities as ents
from backend.vault.models import MappingEntry, Vault, iso_utc
from backend.vault.service import delete_vault, list_entries

router = APIRouter(tags=["mappings"])


class VaultCreate(BaseModel):
    """Anlage einer leeren Zuordnungstabelle."""

    label: str


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, detail="Ungueltige Kennung.") from None


def _get_owned(session: Session, tenant, vault_id: str) -> Vault:
    """Laedt einen Vault und stellt sicher, dass er dem Mandanten gehoert."""
    vault = session.get(Vault, _as_uuid(vault_id))
    if vault is None or vault.tenant_id != tenant.id:
        raise HTTPException(404, detail="Zuordnungstabelle nicht gefunden.")
    return vault


@router.get("/mappings")
async def list_vaults(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> dict:
    """Alle Zuordnungstabellen des Mandanten, neueste zuerst."""
    tenant = get_tenant(session, request)
    rows = session.execute(
        select(Vault, func.count(MappingEntry.id))
        .outerjoin(MappingEntry, MappingEntry.vault_id == Vault.id)
        .where(Vault.tenant_id == tenant.id)
        .group_by(Vault.id)
        .order_by(Vault.created_at.desc())
    ).all()
    return {
        "mappings": [
            {
                "id": str(v.id),
                "label": v.label,
                "created_at": iso_utc(v.created_at),
                "expires_at": iso_utc(v.expires_at),
                "entry_count": int(count),
            }
            for v, count in rows
        ]
    }


@router.post("/mappings", status_code=201)
async def create_vault(
    request: Request,
    payload: VaultCreate,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Legt eine leere Zuordnungstabelle an."""
    tenant = get_tenant(session, request)
    vault = Vault(tenant_id=tenant.id, label=payload.label)
    session.add(vault)
    session.commit()
    session.refresh(vault)
    return {"id": str(vault.id), "label": vault.label, "entry_count": 0}


@router.get("/mappings/{vault_id}")
async def get_vault(
    request: Request,
    vault_id: str,
    session: Annotated[Session, Depends(get_session)],
    entity_type: Optional[str] = None,
) -> dict:
    """Zeigt die Eintraege einer Zuordnungstabelle — ohne die Originale."""
    tenant = get_tenant(session, request)
    vault = _get_owned(session, tenant, vault_id)
    entries = list_entries(session, vault, entity_type)
    return {
        "id": str(vault.id),
        "label": vault.label,
        "created_at": iso_utc(vault.created_at),
        "entries": [
            {
                "id": str(e.id),
                "entity_type": e.entity_type,
                "label": ents.get_label(e.entity_type),
                "pseudonym": e.pseudonym,
                "created_at": iso_utc(e.created_at),
            }
            for e in entries
        ],
    }


@router.delete("/mappings/{vault_id}")
async def remove_vault(
    request: Request, vault_id: str, session: Annotated[Session, Depends(get_session)]
) -> dict:
    """Loescht eine Zuordnungstabelle samt Eintraegen. Nur fuer Administratoren.

    Das ist endgueltig: danach lassen sich die mit diesem Vault erzeugten
    Dokumente nie wieder zurueckfuehren. Deshalb an die CRM-Rolle gebunden und
    nicht an blossen Tool-Zugriff.

    **Ein API-Schluessel kommt hier bewusst NICHT durch** — die einzige
    Ausnahme von der Vollberechtigung des Schluessels (Design-Spec Abschnitt
    4.4). Grund: das Loeschen ist die einzige unumkehrbare Operation der
    Anwendung. Ein geleakter oder verlorener Schluessel darf nicht die
    Rueckfuehrbarkeit einer ganzen Organisation vernichten koennen; alles
    andere, was ein Schluessel anrichtet, laesst sich nachtraeglich korrigieren.
    Das Pflegen der Begriffsliste (``terms.py``) bleibt deshalb erlaubt.
    """
    if has_api_key(request):
        raise HTTPException(
            403,
            detail=(
                "Zuordnungstabellen loeschen ist nur Administratoren in der "
                "Oberflaeche vorbehalten — nicht ueber API-Schluessel."
            ),
        )
    if not is_admin(request):
        raise HTTPException(
            403,
            detail=(
                "Zuordnungstabellen loeschen darf nur, wer im CRM "
                "die Rolle 'admin' hat."
            ),
        )
    tenant = get_tenant(session, request)
    vault = _get_owned(session, tenant, vault_id)
    removed = delete_vault(session, vault)
    session.commit()
    return {"deleted": True, "removed_entries": removed}
