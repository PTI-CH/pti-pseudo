"""Querschnitts-Bausteine des Backends (Middlewares, Hilfsfunktionen).

Heisst bewusst ``core`` und nicht ``lib``: Presidios Root-``.gitignore`` enthaelt
``lib/``, ein ``backend/lib/`` waere unbemerkt nie eingecheckt worden. Vor dem
Anlegen weiterer Ordner darum ``git check-ignore -v <pfad>`` pruefen — ebenfalls
betroffen waeren ``build/``, ``dist/``, ``var/``, ``parts/`` und ``wheels/``.
"""

from backend.core.https_redirect import HttpsRedirectMiddleware
from backend.core.hub_access_auth import (
    HubAccessAuthMiddleware,
    is_admin,
    read_refresh_token,
)

__all__ = [
    "HttpsRedirectMiddleware",
    "HubAccessAuthMiddleware",
    "is_admin",
    "read_refresh_token",
]
