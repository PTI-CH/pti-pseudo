"""Begriffsliste — Ebene 0 der Erkennung.

Eigene Begriffe (Projektnamen, interne Bezeichnungen, Deckbezeichnungen), die
immer ersetzt werden. Sie schlagen jede Heuristik: was hier steht, ist eine
ausdrueckliche Entscheidung eines Menschen und bekommt darum die volle
Konfidenz.

Genau diese Liste macht den Unterschied zwischen einem generischen Erkenner und
einem, der die Eigenheiten einer Organisation kennt.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.api_key_auth import has_api_key
from backend.core.deps import get_session, get_tenant
from backend.core.hub_access_auth import is_admin
from backend.engine import entities as ents
from backend.vault.models import CustomTerm

router = APIRouter(tags=["terms"])

#: Erlaubte Vergleichsarten. ``exact`` achtet auf Gross-/Kleinschreibung,
#: ``normalized`` gleicht Umlautschreibweisen und Leerraum an.
MATCH_MODES = ("exact", "normalized")


class TermCreate(BaseModel):
    """Neuer Eintrag der Begriffsliste."""

    term: str = Field(..., min_length=2, max_length=500)
    entity_type: str = Field("PERSON")
    match_mode: str = Field("normalized")

    @field_validator("term")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Der Begriff darf nicht leer sein.")
        return v

    @field_validator("match_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in MATCH_MODES:
            raise ValueError(f"match_mode muss einer von {MATCH_MODES} sein.")
        return v


@router.get("/terms")
async def list_terms(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> dict:
    """Die Begriffsliste des Mandanten."""
    tenant = get_tenant(session, request)
    rows = session.execute(
        select(CustomTerm)
        .where(CustomTerm.tenant_id == tenant.id)
        .order_by(CustomTerm.term)
    ).scalars()
    return {
        "terms": [
            {
                "id": str(r.id),
                "term": r.term,
                "entity_type": r.entity_type,
                "label": ents.get_label(r.entity_type),
                "match_mode": r.match_mode,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "entity_types": [
            {"value": e, "label": ents.get_label(e)} for e in ents.ALL_ENTITIES
        ]
        if hasattr(ents, "ALL_ENTITIES")
        else [],
    }


@router.post("/terms", status_code=201)
async def create_term(
    request: Request,
    payload: TermCreate,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Nimmt einen Begriff in die Liste auf. Schreiben nur fuer Administratoren.

    Lesen darf jeder mit Tool-Zugriff — die Liste steuert aber, was bei ALLEN
    Nutzern ersetzt wird, und ist damit eine organisationsweite Einstellung.
    Dieselbe Trennung wie beim Glossar im pti-translator.

    Ein API-Schluessel gilt innerhalb seines Mandanten als vollberechtigt
    (Design-Spec Abschnitt 4.4): die aufrufende Anwendung soll ihre eigenen
    Begriffe pflegen koennen, ohne dass ein Mensch dafuer die Oberflaeche
    oeffnet.
    """
    if not (is_admin(request) or has_api_key(request)):
        raise HTTPException(
            403,
            detail=(
                "Die Begriffsliste aendern darf nur, wer im CRM die Rolle 'admin' hat."
            ),
        )
    tenant = get_tenant(session, request)

    exists = session.execute(
        select(CustomTerm).where(
            CustomTerm.tenant_id == tenant.id, CustomTerm.term == payload.term
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, detail="Dieser Begriff steht bereits in der Liste.")

    row = CustomTerm(
        tenant_id=tenant.id,
        term=payload.term,
        entity_type=payload.entity_type,
        match_mode=payload.match_mode,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "id": str(row.id),
        "term": row.term,
        "entity_type": row.entity_type,
        "label": ents.get_label(row.entity_type),
        "match_mode": row.match_mode,
    }


@router.delete("/terms/{term_id}")
async def delete_term(
    request: Request, term_id: str, session: Annotated[Session, Depends(get_session)]
) -> dict:
    """Entfernt einen Begriff. Nur fuer Administratoren.

    Wie beim Anlegen kommt auch ein API-Schluessel durch — siehe
    ``create_term``.
    """
    if not (is_admin(request) or has_api_key(request)):
        raise HTTPException(
            403,
            detail=(
                "Die Begriffsliste aendern darf nur, wer im CRM die Rolle 'admin' hat."
            ),
        )
    tenant = get_tenant(session, request)
    try:
        key = uuid.UUID(str(term_id))
    except (ValueError, TypeError):
        raise HTTPException(400, detail="Ungueltige Kennung.") from None

    row = session.get(CustomTerm, key)
    if row is None or row.tenant_id != tenant.id:
        raise HTTPException(404, detail="Begriff nicht gefunden.")
    session.delete(row)
    session.commit()
    return {"deleted": True}
