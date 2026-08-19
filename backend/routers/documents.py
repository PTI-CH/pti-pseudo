"""REST-Endpunkte fuer Erkennung, Pseudonymisierung und Rueckfuehrung.

Pfade nach Fachkonzept Abschnitt 4 (``documents:<aktion>``). Jeder Endpunkt
nimmt entweder JSON mit ``text`` oder ein Multipart-Formular mit ``file``
entgegen — die Oberflaeche benutzt beides.

Der Router traegt **kein** eigenes Praefix: ``backend/main.py`` haengt ihn
zweimal ein — unter ``/api/v1`` hinter dem SSO-Gate und unter ``/api/ext/v1``
hinter der Schluessel-Pruefung. Gleiche Handler, gleiche Vertraege, zwei
Zugangswege.

Was hier bewusst NICHT passiert: es werden keine Dokument-Bytes gespeichert.
Die Verarbeitung ist ephemer, nur die Zuordnung landet verschluesselt im Vault
(Fachkonzept Abschnitt 9).
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import pipeline
from backend.adapters import UnsupportedFormatError, supported_formats
from backend.config import get_settings
from backend.core.deps import current_username, get_session, get_tenant
from backend.engine import entities as ents
from backend.vault.models import CustomTerm, UsageEvent, Vault

router = APIRouter(tags=["documents"])


class TextRequest(BaseModel):
    """Anfrage fuer die Text-Variante der Endpunkte."""

    text: str = Field(..., description="Der zu verarbeitende Text.")
    mode: str = Field(
        "reversible",
        description=(
            "reversible = Vault anlegen und rueckfuehrbar; anonymous = endgueltig."
        ),
    )
    vault_id: Optional[str] = Field(
        None,
        description="Bestehenden Vault weiterverwenden statt einen neuen anzulegen.",
    )
    vault_label: Optional[str] = Field(
        None, description="Bezeichnung eines neuen Vaults."
    )
    selected_entities: Optional[list[dict]] = Field(
        None,
        description=(
            "Vom Menschen bestaetigte Auswahl aus einem vorherigen detect-Lauf, "
            "je Eintrag segment_id/start/end. Ohne Angabe gilt die Default-Policy."
        ),
    )
    entities: Optional[list[str]] = Field(
        None,
        description=(
            "Explizite Kategorienwahl (Entitaetstypen). Wird sowohl fuer die "
            "Erkennung als auch fuer die Ersetzung verwendet und schlaegt dabei "
            "die Default-Policy — auch KEEP-Typen wie CH_POSTAL_CODE werden "
            "ersetzt, wenn sie hier ausdruecklich gewaehlt sind. Ohne Angabe "
            "gilt die volle Erkennung bzw. die Default-Policy."
        ),
    )


class ReverseTextRequest(BaseModel):
    """Anfrage fuer die Rueckfuehrung eines Textes."""

    text: str
    vault_id: str


def _load_custom_terms(session: Session, tenant_id) -> list[dict]:
    """Begriffsliste des Mandanten als Eingabe fuer die Engine (Layer 0)."""
    rows = session.execute(
        select(CustomTerm).where(CustomTerm.tenant_id == tenant_id)
    ).scalars()
    return [
        {
            "term": r.term,
            "entity_type": r.entity_type,
            "match_mode": r.match_mode,
            "replacement": r.replacement,
        }
        for r in rows
    ]


def _resolve_vault(
    session: Session, tenant, mode: str, vault_id: Optional[str], label: Optional[str]
) -> Optional[Vault]:
    """Bestimmt den Vault fuer diesen Lauf, oder ``None`` im anonymen Modus."""
    if mode == "anonymous":
        return None
    if mode != "reversible":
        raise HTTPException(
            400, detail=f"Unbekannter Modus '{mode}'. Erlaubt: reversible, anonymous."
        )

    if vault_id:
        vault = session.get(Vault, _as_uuid(vault_id))
        if vault is None or vault.tenant_id != tenant.id:
            raise HTTPException(404, detail="Zuordnungstabelle nicht gefunden.")
        return vault

    vault = Vault(tenant_id=tenant.id, label=label or "Ohne Bezeichnung")
    session.add(vault)
    session.flush()
    return vault


def _as_uuid(value: str):
    """Wandelt einen String in eine UUID und meldet Unsinn als 400, nicht als 500."""
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, detail="Ungueltige Kennung.") from None


def _record_usage(
    session: Session, tenant, username: str, action: str, detection
) -> None:
    """Schreibt einen Nutzungseintrag.

    Erfasst werden ausschliesslich Metadaten und Zahlen — nie Inhalte, nie
    Dateinamen (Fachkonzept Abschnitt 9).
    """
    session.add(
        UsageEvent(
            tenant_id=tenant.id,
            username=username,
            action=action,
            entity_counts=detection.counts_by_type(),
            char_count=detection.char_count,
        )
    )


def _check_size(data: bytes) -> None:
    """Prueft die Obergrenze fuer Uploads.

    Muss zu ``client_max_body_size`` im NGINX des Knotens passen — steht dort
    der 1-MB-Default, sieht der Nutzer statt dieser Meldung ein rohes 413.
    """
    limit = get_settings().max_upload_bytes
    if len(data) > limit:
        raise HTTPException(
            413,
            detail=(
                f"Datei ist groesser als das Limit von {limit // (1024 * 1024)} MiB."
            ),
        )


def _check_text_size(text: str) -> None:
    """Prueft die Obergrenze fuer den JSON-Textpfad.

    Gegenstueck zu ``_check_size`` fuer Datei-Uploads: ohne diese Pruefung
    laeuft ein sehr grosser Text ungebremst in die Erkennungs-Engine und
    bringt ``spaCy`` dort mit einem rohen ``ValueError`` zum Absturz
    (``nlp.max_length``, Default 1'000'000 Zeichen) — dann sieht die
    Aufruferin statt einer sauberen Fehlermeldung ein rohes 500.
    """
    limit = get_settings().max_text_chars
    if len(text) > limit:
        raise HTTPException(
            413,
            detail=f"Text ist laenger als das Limit von {limit} Zeichen.",
        )


def parse_selected(value: Optional[str]) -> Optional[list[dict]]:
    """Liest die Befund-Auswahl aus dem Multipart-Formular.

    Gegenstueck zu ``TextRequest.selected_entities`` fuer den Dateipfad: ein
    Formularfeld kann keine Liste von Objekten tragen, also kommt sie als
    JSON-Zeichenkette. Ohne dieses Feld waere das Abwaehlen eines Fehlalarms in
    der Befundliste bei Dateien wirkungslos — die Oberflaeche zeigte
    Kaestchen, die nichts bewirken.

    Eine leere Liste ist ausdruecklich erlaubt und bedeutet „nichts ersetzen".
    Das ist etwas anderes als ein fehlendes Feld (= Default-Policy) und muss
    unterscheidbar bleiben, sonst kaeme beim Abwaehlen aller Funde das
    vollstaendig pseudonymisierte Dokument zurueck.
    """
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            400, detail="selected_entities ist kein gueltiges JSON."
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(e, dict) for e in parsed):
        raise HTTPException(
            400,
            detail=(
                "selected_entities muss eine Liste von Objekten mit "
                "segment_id/start/end sein."
            ),
        )
    return parsed


def parse_entities(value: list[str] | str | None) -> Optional[list[str]]:
    """Validiert die Kategorienwahl gegen den Katalog.

    Nimmt sowohl die Liste aus dem JSON-Textpfad als auch die Kommaliste aus
    dem Multipart-Formular (``anonymize-file`` kennt keine Listenfelder) und
    bringt beide auf dieselbe Form. Eine leere Liste ist kein "alles" —
    das waere stillschweigend nichts ausgewaehlt zu haben — sondern ein
    Bedienfehler und wird als solcher gemeldet.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]

    if not value:
        raise HTTPException(400, detail="Mindestens eine Kategorie waehlen.")
    unbekannt = [v for v in value if v not in ents.ALL_ENTITIES]
    if unbekannt:
        raise HTTPException(
            400,
            detail=(
                f"Unbekannte Entitaetstypen: {', '.join(unbekannt)}. "
                f"Erlaubt: {', '.join(ents.ALL_ENTITIES)}."
            ),
        )
    return value


@router.post("/documents:detect")
async def detect(
    request: Request,
    payload: TextRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Findet Entitaeten, ohne etwas zu ersetzen oder zu speichern."""
    _check_text_size(payload.text)
    entities = parse_entities(payload.entities)
    tenant = get_tenant(session, request)
    terms = _load_custom_terms(session, tenant.id)
    result = pipeline.detect_in_segments(
        [
            pipeline.Segment(
                id=pipeline.TEXT_SEGMENT_ID, text=payload.text, location=None
            )
        ],
        entities=entities,
        custom_terms=terms,
    )
    _record_usage(session, tenant, current_username(request), "detect", result)
    session.commit()
    return {
        "entities": [f.as_dict() for f in result.findings],
        "char_count": result.char_count,
    }


@router.post("/documents:anonymize")
async def anonymize(
    request: Request,
    payload: TextRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Pseudonymisiert einen Text und legt bei Bedarf den Vault an."""
    _check_text_size(payload.text)
    entities = parse_entities(payload.entities)
    tenant = get_tenant(session, request)
    terms = _load_custom_terms(session, tenant.id)
    vault = _resolve_vault(
        session, tenant, payload.mode, payload.vault_id, payload.vault_label
    )

    text, detection, applied = pipeline.anonymize_text(
        session,
        vault,
        payload.text,
        entities=entities,
        custom_terms=terms,
        selected=payload.selected_entities,
    )
    _record_usage(session, tenant, current_username(request), "anonymize", detection)
    session.commit()

    return {
        "text": text,
        "entities": [f.as_dict() for f in detection.findings],
        "applied": len(applied),
        "vault_id": str(vault.id) if vault else None,
        "mode": payload.mode,
        "char_count": detection.char_count,
    }


@router.post("/documents:anonymize-file")
async def anonymize_file(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File(description="DOCX, TXT oder CSV")],
    mode: Annotated[str, Form()] = "reversible",
    vault_id: Annotated[Optional[str], Form()] = None,
    vault_label: Annotated[Optional[str], Form()] = None,
    entities: Annotated[Optional[str], Form()] = None,
    selected_entities: Annotated[Optional[str], Form()] = None,
) -> dict:
    """Pseudonymisiert ein Dokument formaterhaltend.

    Die Datei kommt als Base64 zurueck statt als Datei-Download, weil die
    Antwort neben dem Dokument auch den Befund tragen muss — die Oberflaeche
    zeigt beides.

    ``selected_entities`` traegt die geprueften Funde als JSON-Zeichenkette
    (siehe :func:`parse_selected`). Ohne dieses Feld gaebe es fuer Dateien
    keinen Weg, einen Fehlalarm abzuwaehlen — die Kaestchen in der
    Befundliste haetten dann keine Wirkung.
    """
    data = await file.read()
    _check_size(data)
    parsed_entities = parse_entities(entities)
    parsed_selected = parse_selected(selected_entities)

    tenant = get_tenant(session, request)
    terms = _load_custom_terms(session, tenant.id)
    vault = _resolve_vault(
        session, tenant, mode, vault_id, vault_label or file.filename
    )

    try:
        out, detection, applied = pipeline.anonymize_document(
            session,
            vault,
            data,
            file.filename or "unbenannt.txt",
            entities=parsed_entities,
            custom_terms=terms,
            selected=parsed_selected,
        )
    except UnsupportedFormatError as exc:
        raise HTTPException(
            415,
            detail=f"{exc} Unterstuetzt werden: {', '.join(supported_formats())}.",
        ) from exc

    _record_usage(
        session, tenant, current_username(request), "anonymize_file", detection
    )
    session.commit()

    return {
        "filename": file.filename,
        "content_base64": base64.b64encode(out).decode("ascii"),
        "entities": [f.as_dict() for f in detection.findings],
        "applied": len(applied),
        "vault_id": str(vault.id) if vault else None,
        "mode": mode,
        "char_count": detection.char_count,
    }


@router.post("/documents:reverse")
async def reverse(
    request: Request,
    payload: ReverseTextRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Fuehrt einen pseudonymisierten Text zurueck.

    Achtung: hier entstehen wieder Klartext-Personendaten. Die Oberflaeche
    weist darauf hin.

    Die Textgrenze gilt auch hier: die Rueckfuehrung laeuft zwar nicht durch
    spaCy, aber ein unbegrenzter Text wuerde die Ersetzung ueber den ganzen
    Vault-Bestand ungebremst laufen lassen. Dieselbe Grenze wie bei ``detect``
    und ``anonymize``, damit die Aufruferin nur eine Zahl kennen muss.
    """
    _check_text_size(payload.text)
    tenant = get_tenant(session, request)
    vault = session.get(Vault, _as_uuid(payload.vault_id))
    if vault is None or vault.tenant_id != tenant.id:
        raise HTTPException(404, detail="Zuordnungstabelle nicht gefunden.")

    text = pipeline.reverse_text(session, vault, payload.text)
    session.add(
        UsageEvent(
            tenant_id=tenant.id,
            username=current_username(request),
            action="reverse",
            entity_counts=None,
            char_count=len(payload.text),
        )
    )
    session.commit()
    return {"text": text, "vault_id": str(vault.id)}


@router.post("/documents:reverse-file")
async def reverse_file(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    vault_id: Annotated[str, Form()],
) -> dict:
    """Fuehrt ein pseudonymisiertes Dokument formaterhaltend zurueck."""
    data = await file.read()
    _check_size(data)

    tenant = get_tenant(session, request)
    vault = session.get(Vault, _as_uuid(vault_id))
    if vault is None or vault.tenant_id != tenant.id:
        raise HTTPException(404, detail="Zuordnungstabelle nicht gefunden.")

    try:
        out = pipeline.reverse_document(
            session, vault, data, file.filename or "unbenannt.txt"
        )
    except UnsupportedFormatError as exc:
        raise HTTPException(415, detail=str(exc)) from exc

    session.add(
        UsageEvent(
            tenant_id=tenant.id,
            username=current_username(request),
            action="reverse_file",
            entity_counts=None,
            char_count=len(data),
        )
    )
    session.commit()
    return {
        "filename": file.filename,
        "content_base64": base64.b64encode(out).decode("ascii"),
        "vault_id": str(vault.id),
    }
