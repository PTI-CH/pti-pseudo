"""Gemeinsame FastAPI-Abhaengigkeiten: Datenbanksitzung, Mandant, Benutzer.

Zur Mandantenfaehigkeit: das Fachkonzept sieht echte Mehrmandantenfaehigkeit
mit API-Schluesseln vor (Abschnitt 8), aber erst ab Phase 2. In Phase 1 gibt es
genau einen Mandanten, und alle Tabellen tragen trotzdem schon ``tenant_id``.
Das ist Absicht: die Spalte nachtraeglich einzuziehen waere eine Migration ueber
den gesamten Vault-Bestand, sie jetzt mitzufuehren kostet nichts.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import get_sessionmaker
from backend.vault.models import Tenant

#: Name des Phase-1-Mandanten. Wird beim ersten Zugriff angelegt.
DEFAULT_TENANT_NAME = "PTI"


def get_session() -> Iterator[Session]:
    """Datenbanksitzung je Request, die am Ende zuverlaessig geschlossen wird."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_tenant(session: Session) -> Tenant:
    """Liefert den Phase-1-Mandanten und legt ihn beim ersten Mal an."""
    tenant = session.execute(
        select(Tenant).where(Tenant.name == DEFAULT_TENANT_NAME)
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=DEFAULT_TENANT_NAME)
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
    return tenant


def current_user(request: Request) -> dict:
    """Der vom SSO-Gate hinterlegte Benutzer.

    Das Gate laeuft als Middleware vor jeder gegateten Route und setzt
    ``request.state.hub_user``. Ist dort nichts, wurde die Route faelschlich
    ausgenommen — dann lieber ein leeres Profil als ein Absturz, denn der
    Zugriffsschutz haengt nicht an dieser Funktion, sondern am Gate.
    """
    user = getattr(request.state, "hub_user", None)
    return user if isinstance(user, dict) else {}


def current_username(request: Request) -> str:
    """Stabiler Benutzerschluessel fuer die Nutzungserfassung.

    Bewusst ``username`` und nicht die UUID: ``user.person`` kann im CRM
    ``null`` sein, und ein Top-Level-``email`` gibt es dort nicht.
    """
    return str(current_user(request).get("username") or "unbekannt")
