"""FastAPI-Router des Backends.

Die fachlichen Router haengen spaeter unter ``/api/v1``; ``health`` liegt
bewusst daneben auf ``/api/health``, weil der Pfad in Deploy-Skripten, im
Loadbalancer und im Smoke-Test fest verdrahtet ist und keine Version tragen
soll.
"""

from backend.routers import health

__all__ = ["health"]
