"""PTI-Pseudonymisierer — Backend-Paket.

Enthaelt die FastAPI-Anwendung, die Engine, den Vault und die Middlewares.
Presidio selbst liegt bewusst ausserhalb dieses Pakets (Editable-Install aus
``presidio-analyzer/`` und ``presidio-anonymizer/``).
"""

# Wird von ``GET /api/health`` ausgeliefert und dient dem Deploy-Smoke-Test als
# Nachweis, welcher Stand tatsaechlich laeuft.
__version__ = "0.1.0"

__all__ = ["__version__"]
