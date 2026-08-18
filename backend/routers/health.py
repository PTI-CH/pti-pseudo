"""Health-Endpunkt fuer Loadbalancer und Deploy-Smoke-Test.

Bewusst anspruchslos: **keine DB-Abfrage, kein geladenes NLP-Modell**. Der
Smoke-Test nach dem Deploy laeuft, waehrend die Engine das spaCy-Modell noch in
den Speicher zieht (rund 1.1 s je Worker) — ein Health-Check, der auf die Engine
wartete, meldete in genau diesem Fenster faelschlich "unten".

Der Endpunkt ist zusaetzlich vom SSO-Gate und vom HTTPS-Redirect ausgenommen,
sonst saehe der Smoke-Test nur 302 bzw. 301.

PTI-Hausstandard ist exakt ``{"ok": true}`` als Teilmenge der Antwort; die
weiteren Felder sind Zugabe zur Diagnose, welcher Stand tatsaechlich laeuft.
"""

from fastapi import APIRouter

from backend import __version__

router = APIRouter(tags=["health"])

SERVICE_NAME = "pti-pseudonymisierer"


@router.get("/api/health")
async def health() -> dict[str, object]:
    """Meldet, dass der Prozess Requests annimmt.

    Returns
    -------
    dict
        ``{"ok": True, "service": ..., "version": ...}``.
    """
    return {"ok": True, "service": SERVICE_NAME, "version": __version__}


__all__ = ["SERVICE_NAME", "router"]
