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
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import pipeline
from backend.adapters import UnsupportedFormatError, get_adapter, supported_formats
from backend.config import get_settings
from backend.core.deps import current_actor, current_username, get_session, get_tenant
from backend.core.kennungen import als_uuid
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
    ki_pruefung: bool = Field(
        False,
        description=(
            "KI-gestuetzte Pruefung der Namensgrenzen ueber einen Schweizer "
            "Dienst (Infomaniak AI, Variante C). Dabei verlassen kurze "
            "Textausschnitte um die gefundenen Stellen den Server. "
            "Vorgabe AUS: Ein Maschinen-Aufrufer muss den Datenversand "
            "ausdruecklich anfordern, sonst begaennen andere PTI-Anwendungen "
            "ab einem Deploy stillschweigend damit."
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


def _as_uuid(value: str) -> uuid.UUID:
    """Wandelt einen String in eine UUID und meldet Unsinn als 400, nicht als 500."""
    kennung = als_uuid(value)
    if kennung is None:
        raise HTTPException(400, detail="Ungültige Kennung.")
    return kennung


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
                f"Datei ist grösser als das Limit von {limit // (1024 * 1024)} MiB."
            ),
        )


def _laeuft_als_auftrag(data: bytes, dateiname: str) -> bool:
    """Ob diese Datei als Auftrag laufen muss statt im offenen Request.

    **Zwei Schranken, und ihre Reihenfolge ist tragend.**

    1. **Bytes** (:attr:`Settings.sync_threshold_bytes`), ohne die Datei zu
       oeffnen. Grob, aber unverzichtbar: Eine sehr grosse Datei soll gar
       nicht erst durch den Adapter, und fuer eine beschaedigte Datei ist
       das die einzige Schranke, die ueberhaupt greift -- ``extract_segments``
       wuerde daran werfen, und der Auftragspfad ist genau der Ort, an dem
       ein solcher Fehler sauber als gescheiterter Auftrag endet statt als
       500 mitten in einer Anfrage.
    2. **Zeichen** (:attr:`Settings.sync_threshold_chars`), nach dem
       Adapterlauf. Die scharfe Schranke.

    **Warum es die zweite ueberhaupt braucht.** Am 2026-08-28 gemessen
    (``scripts/schwelle_messen.py``): Die Dauer ist linear im TEXT, rund
    20'000 Zeichen je Sekunde ueber alle Formate. Die Dateigroesse sagt
    darueber fast nichts -- dieselben 64'107 Zeichen wiegen als txt 64 KiB,
    als docx 39 KiB und als xlsx 8 KiB. Mit der Byte-Schranke allein war
    eine dichte Arbeitsmappe von 2 MiB rund 15 Minuten unterwegs -- weit
    jenseits dessen, was der Balancer offen haelt: Die Schwelle schuetzte
    nicht, wovor sie schuetzen sollte.

    **Der Wert von ``sync_threshold_chars`` ist seit dem 2026-08-28
    gemessen abgeleitet**, nicht mehr angenommen. Das Zeitbudget des
    Balancers betraegt 300 Sekunden (ein Lauf ueber 280'000 Zeichen bekam
    nach 300.5 s einen ``504``); frueher stand hier eine aus
    ``proxy_read_timeout``s NGINX-Vorgabe geratene Minute. Die Schwelle
    steht auf 150'000 -- gerechnet mit dem langsamsten gemessenen FORMAT.
    Eine erste Fassung stand auf 400'000 und war zu hoch: Dieselbe
    Zeichenzahl kostet als xlsx rund das Sechsfache wie als txt (ein
    ``.txt`` ist EIN Segment, 100'000 Zeichen als xlsx sind 427 -- je
    Segment ein spaCy-Lauf). 200'000 Zeichen als xlsx rissen die Grenze mit
    einem ``504``.
    Zahlen: Design-Spec zu den asynchronen Auftraegen, §12. Die
    Grundaussage haengt nicht daran: 15 Minuten sind unter jeder denkbaren
    Zeitgrenze zu lang.

    **Der Preis ist gemessen und klein.** Der Adapterlauf kostet 0.0 bis
    4.9 % der Gesamtdauer, mit fallendem Anteil bei groesseren Dokumenten --
    genau bei den Dateien, bei denen die Entscheidung zaehlt, ist das
    Oeffnen fast gratis. Im synchronen Zweig laeuft er dadurch zweimal
    (hier und in der Pipeline); ihn durchzureichen haette die Signatur von
    ``anonymize_document`` fuer wenige Prozent verbogen.

    ``UnsupportedFormatError`` wird **nicht** gefangen: Der Aufrufer
    uebersetzt sie in dieselbe 415 wie bisher, unabhaengig von der
    Dateigroesse.
    """
    einstellungen = get_settings()
    if len(data) > einstellungen.sync_threshold_bytes:
        return True
    adapter = get_adapter(dateiname)
    zeichen = sum(len(s.text) for s in adapter.extract_segments(data))
    return zeichen > einstellungen.sync_threshold_chars


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
            detail=f"Text ist länger als das Limit von {limit} Zeichen.",
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
            400, detail="selected_entities ist kein gültiges JSON."
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
        raise HTTPException(400, detail="Mindestens eine Kategorie wählen.")
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
        ki_pruefung=payload.ki_pruefung,
    )
    _record_usage(session, tenant, current_username(request), "detect", result)
    session.commit()
    return {
        "entities": [f.as_dict() for f in result.findings],
        "char_count": result.char_count,
        "ki_geprueft": result.ki_geprueft,
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
        ki_pruefung=payload.ki_pruefung,
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
        "ki_geprueft": detection.ki_geprueft,
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
    ki_pruefung: Annotated[bool, Form()] = False,
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

    # Oberhalb der Schwelle wird daraus ein Auftrag: Ein grosses Dokument
    # sprengt den Request, bevor der Balancer ihn zu Ende laufen laesst.
    # Welche zwei Schranken das entscheiden und warum es zwei sind, steht in
    # `_laeuft_als_auftrag`.
    #
    # Die TEXTrouten bleiben davon unberuehrt — sie sind durch `max_text_chars`
    # begrenzt und laufen in Sekundenbruchteilen. Sie einzubeziehen gaebe der
    # haeufigsten Nutzung ohne Not einen Umweg.
    #
    # Die Formatabsage kommt hier schon aus der Entscheidung selbst (sie
    # oeffnet die Datei) und nicht erst aus `lege_an` bzw. der Pipeline —
    # dieselbe 415, nur frueher. Der `except`-Zweig unten deckt beide Wege
    # ab, weil er den ganzen Block umschliesst.
    # Funktionslokal wegen des Zirkels adapters -> jobs.ausfuehrung ->
    # pipeline -> adapters (siehe pdf_adapter._segmente_fuer_bildseite).
    from backend.jobs.ausfuehrung import NutzermeldungError

    try:
        als_auftrag = _laeuft_als_auftrag(data, file.filename or "unbenannt.txt")
    except UnsupportedFormatError as exc:
        raise HTTPException(
            415,
            detail=f"{exc} Unterstützt werden: {', '.join(supported_formats())}.",
        ) from exc
    except NutzermeldungError as exc:
        # Ein PDF mit gescannten Seiten, dessen Text ohne OCR niemand lesen
        # kann: `_laeuft_als_auftrag` ruft `extract_segments`, um Zeichen zu
        # zaehlen, und der PdfAdapter wirft hier LAUT statt ein scheinbar
        # sauberes, aber unverarbeitetes Dokument zu liefern (Spec §4.3, die
        # tragende Sicherheitsregel). 415 wie fuer ein nicht verarbeitbares
        # Format; der Text der Ausnahme nennt den naechsten Schritt. Grosse
        # Scans ueber der Byte-Schranke erreichen diese Stelle nicht — sie
        # werden zum Auftrag und scheitern sauber im Arbeiter.
        raise HTTPException(415, detail=str(exc)) from exc

    if als_auftrag:
        from backend.jobs import warteschlange

        try:
            job = warteschlange.lege_an(
                session,
                tenant=get_tenant(session, request),
                aktor=current_actor(request),
                mode="anonymize",
                # Derselbe Rueckfall wie der synchrone Pfad unten
                # (``file.filename or "unbenannt.txt"``). Frueher stand hier
                # "dokument", ohne Endung — mit der Formatpruefung in
                # ``lege_an`` haette eine Datei ohne Namen jetzt 415 bekommen,
                # waehrend dieselbe Datei unterhalb der Schwelle als Text
                # durchgeht.
                dateiname=file.filename or "unbenannt.txt",
                daten=data,
                parameter={
                    "modus": mode,
                    "vault_id": vault_id,
                    # Dieselbe Rueckfallregel wie im synchronen Pfad unten
                    # (``_resolve_vault(..., vault_label or file.filename)``).
                    # Die Oberflaeche schickt vault_label nie mit -- ohne den
                    # Rueckfall hiesse jeder ueber einen Auftrag angelegte
                    # Tresor "Ohne Bezeichnung", und in der Tresorliste liesse
                    # sich nicht mehr sagen, welche Tabelle zu welchem
                    # Dokument gehoert.
                    "vault_label": vault_label or file.filename,
                    "entities": parsed_entities,
                    "selected_entities": parsed_selected,
                    "ki_pruefung": ki_pruefung,
                },
            )
        except UnsupportedFormatError as exc:
            # Dieselbe 415 wie unten im synchronen Zweig — der Punkt der
            # Pruefung in ``lege_an`` ist, dass die Absage NICHT von der
            # Dateigroesse abhaengt.
            raise HTTPException(
                415,
                detail=f"{exc} Unterstützt werden: {', '.join(supported_formats())}.",
            ) from exc
        except warteschlange.AuftragNichtMoeglichError as exc:
            # 503, nicht 500: es liegt nicht an der Anfrage, sondern daran,
            # dass diese Installation keinen Hintergrundarbeiter faehrt. Ohne
            # diese Abweisung bekaeme die Datei eine job_id, die nie jemand
            # abarbeitet — der Auftrag bliebe fuer immer auf "wartend", und
            # die Oberflaeche fragte endlos nach, ohne dass irgendwo stuende,
            # warum.
            raise HTTPException(503, detail=str(exc)) from exc
        session.commit()
        return JSONResponse(
            status_code=202,
            content={"job_id": str(job.id), "status": job.status},
        )

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
            ki_pruefung=ki_pruefung,
        )
    except UnsupportedFormatError as exc:
        raise HTTPException(
            415,
            detail=f"{exc} Unterstützt werden: {', '.join(supported_formats())}.",
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
        "ki_geprueft": detection.ki_geprueft,
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

    # Dieselbe Entscheidung wie bei `anonymize_file` — eine grosse
    # Rueckfuehrung ist genauso lang unterwegs wie eine grosse
    # Pseudonymisierung. Begruendung der zwei Schranken: `_laeuft_als_auftrag`.
    # Wie bei anonymize_file — siehe dort, auch zum Zirkel.
    from backend.jobs.ausfuehrung import NutzermeldungError

    try:
        als_auftrag = _laeuft_als_auftrag(data, file.filename or "unbenannt.txt")
    except UnsupportedFormatError as exc:
        raise HTTPException(415, detail=str(exc)) from exc
    except NutzermeldungError as exc:
        # Gescanntes PDF ohne OCR -> 415 statt 500 (siehe anonymize_file).
        raise HTTPException(415, detail=str(exc)) from exc

    if als_auftrag:
        from backend.jobs import warteschlange

        try:
            job = warteschlange.lege_an(
                session,
                tenant=get_tenant(session, request),
                aktor=current_actor(request),
                mode="reverse",
                # Derselbe Rueckfall wie der synchrone Pfad unten
                # (``file.filename or "unbenannt.txt"``). Frueher stand hier
                # "dokument", ohne Endung — mit der Formatpruefung in
                # ``lege_an`` haette eine Datei ohne Namen jetzt 415 bekommen,
                # waehrend dieselbe Datei unterhalb der Schwelle als Text
                # durchgeht.
                dateiname=file.filename or "unbenannt.txt",
                daten=data,
                parameter={"vault_id": vault_id},
            )
        except UnsupportedFormatError as exc:
            # Wie bei anonymize_file — siehe dort.
            raise HTTPException(415, detail=str(exc)) from exc
        except warteschlange.AuftragNichtMoeglichError as exc:
            # Dieselbe Abweisung wie bei anonymize_file — siehe dort.
            raise HTTPException(503, detail=str(exc)) from exc
        session.commit()
        return JSONResponse(
            status_code=202,
            content={"job_id": str(job.id), "status": job.status},
        )

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
