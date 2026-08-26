"""Statusabfrage und Abholen fertiger Auftraege.

**Sichtbarkeit:** Ein Aufrufer sieht ausschliesslich seine eigenen Auftraege.
Geprueft wird gegen ``aktor``, nicht nur gegen ``tenant_id`` — ein
API-Schluessel darf die Auftraege eines anderen Schluessels nicht sehen und
erst recht nicht die eines Menschen in der Oberflaeche.

Ein fremder oder unbekannter Auftrag ergibt **404, nicht 403**: Ein 403
verriete, dass es ihn gibt.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.core.deps import current_actor, get_session, get_tenant
from backend.core.kennungen import als_uuid
from backend.vault import crypto
from backend.vault.models import Job

router = APIRouter(tags=["jobs"])


def _as_uuid(value: str) -> uuid.UUID:
    """Wandelt die Pfad-Kennung um — ein ungueltiges Format ist ebenfalls 404.

    Ein Formatfehler soll nicht mehr ueber einen Auftrag verraten als eine
    falsche, aber wohlgeformte Kennung: beides ist fuer den Aufrufer schlicht
    "nicht gefunden".
    """
    kennung = als_uuid(value)
    if kennung is None:
        raise HTTPException(404, detail="Auftrag nicht gefunden.")
    return kennung


def _hole_eigenen(session: Session, request: Request, job_id: str) -> Job:
    """Laedt einen Auftrag und stellt sicher, dass er dem Aufrufer gehoert."""
    tenant = get_tenant(session, request)
    job = session.get(Job, _as_uuid(job_id))
    if (
        job is None
        or job.tenant_id != tenant.id
        or job.aktor != current_actor(request)
    ):
        raise HTTPException(404, detail="Auftrag nicht gefunden.")
    return job


@router.get("/jobs/{job_id}")
async def status(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Status eines Auftrags. Enthaelt **keine** Dokumentinhalte."""
    job = _hole_eigenen(session, request, job_id)
    return {
        "id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "dateiname": job.dateiname,
        "fortschritt": job.fortschritt,
        "gesamt": job.gesamt,
        "fehler": job.fehler,
        "erstellt": job.created_at.isoformat() if job.created_at else None,
        "laeuft_ab": job.expires_at.isoformat() if job.expires_at else None,
    }


@router.get("/jobs/{job_id}/result")
async def ergebnis(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Das fertige Ergebnis — die Form des synchronen Pfads, und zwei Felder mehr.

    ``ergebnis_blob`` traegt seit Aufgabe 9 einen JSON-Umschlag (siehe
    ``warteschlange.melde_fertig``): Dokument als Base64 plus die
    Zusatzangaben, die ``documents.anonymize_file`` bzw. ``reverse_file``
    synchron liefern — Fundliste, angewendete Zahl, Tresor-Kennung. Diese
    Route packt den Umschlag nur aus, statt eine eigene, aehnliche Form zu
    bauen: nur so bekommt der Mensch bei einem grossen Dokument dieselbe
    Fundliste zum Abwaehlen und dieselbe Tresor-Kennung fuer die
    Rueckfuehrung wie beim kleinen.

    **Deckungsgleich ist sie trotzdem nicht — sie ist eine Obermenge.** Ein
    ``anonymize``-Umschlag traegt zusaetzlich ``requested_entities`` und
    ``ki_pruefung``, also womit der Lauf gestartet wurde. Der synchrone
    Aufrufer weiss das selbst; wer einen Auftrag nach einem Seitenneuladen
    wiederaufnimmt, hat nur noch die Auftragskennung. Ohne die beiden Felder
    faellt seine Oberflaeche beim naechsten Schritt ("Anwenden" mit
    abgewaehlten Fehlalarmen) auf ihre Vorgabewerte zurueck — und legt fuer
    einen ``anonymous``-Lauf einen Tresor an, den niemand bestellt hat.

    Wer die Antwort auswertet, darf also weitere Schluessel nicht als Fehler
    behandeln. Aeltere Umschlaege (bis 24 h nach einem Deploy real moeglich)
    haben die beiden umgekehrt noch nicht — sie fehlen dann schlicht.
    """
    job = _hole_eigenen(session, request, job_id)
    if job.status != "fertig":
        raise HTTPException(
            409,
            detail={
                "status": job.status,
                "fehler": job.fehler,
                "meldung": "Der Auftrag ist noch nicht fertig.",
            },
        )
    umschlag = json.loads(crypto.open(str(job.tenant_id), job.ergebnis_blob))
    return {"filename": job.dateiname, **umschlag}
