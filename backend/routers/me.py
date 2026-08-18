"""Wer bin ich — die Rollenquelle fuer die Oberflaeche.

Bewusst datensparsam: nur was die Oberflaeche wirklich zum Anzeigen und zum
Ein-/Ausblenden von Knoepfen braucht. Kein Personendatensatz, keine E-Mail,
keine Personen-Kennung, keine App-Flags.

Der Zugriffsschutz haengt NICHT an dieser Antwort. Sie ist kosmetisch — die
Durchsetzung sitzt im SSO-Gate (Middleware) und in den einzelnen Endpunkten.
Ein manipulierter ``isAdmin``-Wert im Browser bringt darum nichts.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.core.deps import current_user
from backend.core.hub_access_auth import is_admin

router = APIRouter(tags=["me"])


@router.get("/api/me")
async def me(request: Request) -> dict:
    """Angemeldeter Benutzer in der Form, die die Oberflaeche erwartet."""
    user = current_user(request)
    person = user.get("person") or {}
    # user.person kann im CRM null sein — dann bleibt der Anzeigename leer und
    # die Oberflaeche faellt auf den Benutzernamen zurueck.
    name = " ".join(
        part for part in (person.get("firstName"), person.get("lastName")) if part
    ).strip()

    return {
        "username": user.get("username"),
        "name": name or None,
        "role": user.get("role"),
        "isAdmin": is_admin(request),
    }
