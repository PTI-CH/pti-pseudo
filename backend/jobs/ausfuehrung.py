"""Die Naht zwischen Auftrag und Pipeline.

Die Warteschlange weiss nichts von Dokumenten, die Pipeline nichts von
Auftraegen. Hier treffen sie sich — und nur hier. Dadurch bleibt beides fuer
sich pruefbar, und ein neuer Auftragstyp (Zyklus B: OCR) beruehrt genau diese
eine Datei.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from backend.db import get_sessionmaker
from backend.pipeline import anonymize_document, reverse_document
from backend.vault.models import Job, Tenant, Vault

logger = logging.getLogger("pti.jobs.ausfuehrung")


@dataclass
class AuftragsErgebnis:
    """Rueckgabe von :func:`fuehre_aus`: Dokumentbytes plus Zusatzangaben.

    Ein blosses Bytes-Objekt genuegte, solange der Auftragspfad nur das
    fertige Dokument lieferte. Seit Aufgabe 9 muss ``warteschlange.
    melde_fertig`` denselben Umfang versiegeln, den der synchrone Pfad in
    seiner Antwort zeigt (Fundliste, angewendete Zahl, Tresor-Kennung) — sonst
    kann der Mensch bei einem grossen Dokument keinen Fehlalarm abwaehlen und
    ein umkehrbar verarbeitetes Dokument bleibt ohne die Tresor-Kennung fuer
    immer unrueckfuehrbar.

    ``zusatz`` ist ein Dict statt eigener Felder, weil sich der Umfang je
    Auftragstyp unterscheidet: ``anonymize`` traegt Funde, angewendete Zahl,
    Modus, Zeichenzahl und KI-Status; ``reverse`` nur die Tresor-Kennung. Ein
    Tupel mit fester Stellenzahl haette diesen Unterschied nicht sauber
    abgebildet.
    """

    daten: bytes
    zusatz: dict = field(default_factory=dict)


def fuehre_aus(job: Job, daten: bytes, melde_fortschritt) -> AuftragsErgebnis:
    """Fuehrt einen Auftrag aus und gibt Dokumentbytes plus Zusatzangaben zurueck.

    ``melde_fortschritt(erledigt, gesamt)`` wird von Formaten aufgerufen, die
    eine Seitenzahl kennen. Fuer docx und xlsx gibt es keine — dort bleibt es
    bei „laeuft", und das ist ehrlicher als ein erfundener Balken.
    """
    parameter = job.parameter_json or {}
    dateiname = job.dateiname or "dokument"
    # Zaehlbares, kein Dateiname und kein Inhalt: schliesst die Luecke
    # zwischen "angelegt" (warteschlange.lege_an) und "fertig"/"gescheitert"
    # (warteschlange.melde_fertig/melde_fehler) — ohne diese Zeile ist ein
    # lang laufender Auftrag im Protokoll unsichtbar, solange er laeuft.
    logger.info("Auftrag wird ausgefuehrt: %s, %d Byte", job.mode, len(daten))

    with get_sessionmaker()() as session:
        tenant = session.get(Tenant, job.tenant_id)

        if job.mode == "reverse":
            # Hier wiegt eine fehlende Pruefung schwerer als beim
            # Pseudonymisieren: ein fremder Tresor setzte nicht bloss falsche
            # Pseudonyme, sondern die KLARNAMEN eines fremden Mandanten in
            # dieses Dokument ein. Ohne vault_id gibt es zudem nichts
            # zurueckzufuehren — auch das muss verstaendlich scheitern statt
            # mit einem rohen KeyError.
            vault_id = parameter.get("vault_id")
            if not vault_id:
                raise ValueError(
                    "Fuer die Rueckfuehrung wird eine vault_id benoetigt."
                )
            vault = _lade_bestehenden_vault(session, tenant, vault_id)
            ergebnis = reverse_document(session, vault, daten, dateiname)
            session.commit()
            # Dieselbe knappe Form wie ``documents.reverse_file``: dort nur
            # Dateiname, Inhalt und Tresor-Kennung — keine Funde, weil die
            # Rueckfuehrung keine neue Erkennung ausfuehrt.
            return AuftragsErgebnis(daten=ergebnis, zusatz={"vault_id": str(vault.id)})

        if job.mode == "anonymize":
            # Die Begriffsliste MUSS mit. Ohne sie liefe der Auftrag still
            # ohne Ebene 0 — dieselben Behoerdennamen, die im synchronen Pfad
            # gefunden werden, blieben hier im Klartext stehen. Der Fehler
            # waere unsichtbar: Das Ergebnis saehe pseudonymisiert aus.
            # Funktionsinterner Import mit Absicht: `documents.py` bindet ab
            # Aufgabe 8 seinerseits `backend.jobs` ein. Auf Modulebene waere
            # das ein Zirkel; documents.py macht es in die andere Richtung
            # genauso.
            from backend.routers.documents import _load_custom_terms

            begriffe = _load_custom_terms(session, tenant.id)
            vault = _hole_vault(session, tenant, parameter)
            ergebnis, befund, gewaehlt = anonymize_document(
                session,
                vault,
                daten,
                dateiname,
                entities=parameter.get("entities"),
                custom_terms=begriffe,
                selected=parameter.get("selected_entities"),
                ki_pruefung=bool(parameter.get("ki_pruefung")),
            )
            session.commit()
            # Dieselben Schluessel wie die Antwort des synchronen Pfads
            # (``documents.anonymize_file``) — die Ergebnisroute uebernimmt
            # sie woertlich, damit beide Wege dieselbe Form liefern.
            zusatz = {
                "entities": [f.as_dict() for f in befund.findings],
                "applied": len(gewaehlt),
                "vault_id": str(vault.id) if vault else None,
                "mode": parameter.get("modus"),
                "char_count": befund.char_count,
                "ki_geprueft": befund.ki_geprueft,
            }
            return AuftragsErgebnis(daten=ergebnis, zusatz=zusatz)

    raise ValueError(f"Unbekannter Auftragstyp: {job.mode}")


def _hole_vault(session, tenant, parameter: dict) -> Optional[Vault]:
    """Bestimmt den Vault fuer einen Anonymisierungs-Auftrag.

    Bildet nach, was ``documents._resolve_vault`` fuer den synchronen Pfad
    tut (``backend/routers/documents.py:108-125``): im Modus ``anonymous``
    gibt es keinen Vault, im Modus ``reversible`` wird ein bestehender per
    ``vault_id`` weiterverwendet oder ein neuer mit ``vault_label`` angelegt.
    Jeder andere Modus ist ein Fehler.

    Zwei Pruefungen, die die Vorlage hat und die hier nicht fehlen duerfen —
    ein Auftrag entsteht ab Aufgabe 8 aus Nutzereingaben, beide sind
    sicherheitsrelevant:

    * **Mandantenprüfung.** Ein ``vault_id`` ohne Abgleich gegen
      ``tenant.id`` liesse einen Auftrag den Tresor eines FREMDEN Mandanten
      benutzen — dessen Zuordnung Original->Pseudonym auf ein fremdes
      Dokument anwenden. Genau das soll der Vault verhindern.
    * **Moduspruefung.** Ohne sie landet ein Tippfehler im Modus
      (``"anonymus"``, ``"reversibel"``) still im umkehrbaren Zweig — ein
      Dokument, das der Mensch fuer endgueltig anonymisiert haelt, waere
      rueckfuehrbar.

    Anders als ``_resolve_vault`` wirft diese Funktion kein
    ``HTTPException``: ein Auftrag laeuft nicht in einer Anfrage. Ein
    ``ValueError`` mit klarer Meldung ist richtig — der ``Arbeiter`` faengt
    ihn und setzt den Auftrag auf ``fehler``; die Meldung landet fuer den
    Menschen sichtbar in ``job.fehler``.
    """
    modus = parameter.get("modus")
    if modus == "anonymous":
        return None
    if modus != "reversible":
        raise ValueError(
            f"Unbekannter Modus '{modus}'. Erlaubt: reversible, anonymous."
        )

    vault_id = parameter.get("vault_id")
    if vault_id:
        return _lade_bestehenden_vault(session, tenant, vault_id)

    vault = Vault(
        tenant_id=tenant.id, label=parameter.get("vault_label") or "Ohne Bezeichnung"
    )
    session.add(vault)
    session.flush()
    return vault


def _lade_bestehenden_vault(session, tenant, vault_id) -> Vault:
    """Laedt einen bestehenden Vault und prueft die Mandantenzugehoerigkeit.

    Gemeinsam genutzt vom reversiblen Zweig von :func:`_hole_vault`
    (Pseudonymisierung) und vom ``reverse``-Zweig in :func:`fuehre_aus`
    (Rueckfuehrung) — zwei Fassungen derselben Pruefung waeren zwei Stellen,
    die auseinanderlaufen koennten. Bei der Rueckfuehrung wiegt ein
    uebersprungener Mandantenabgleich am schwersten: dort wuerden die
    KLARNAMEN eines fremden Mandanten in ein Dokument eingesetzt, nicht nur
    falsche Pseudonyme vergeben.

    ``parameter_json`` ist JSON — eine UUID kommt hier immer als
    Zeichenkette an. ``session.get`` auf einer ``Uuid``-Spalte verlangt
    (jedenfalls auf SQLite, dem Testbackend) ein echtes ``uuid.UUID``-Objekt;
    dieselbe Umwandlung wie ``documents._as_uuid`` fuer den synchronen Pfad,
    nur mit ``ValueError`` statt ``HTTPException`` — ein Auftrag laeuft nicht
    in einer Anfrage, und der ``Arbeiter`` faengt ``ValueError`` und setzt
    den Auftrag auf ``fehler``; die Meldung landet fuer den Menschen sichtbar
    in ``job.fehler``.
    """
    try:
        vault_uuid = uuid.UUID(str(vault_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Ungueltige Zuordnungstabellen-Kennung.") from exc
    vault = session.get(Vault, vault_uuid)
    if vault is None or vault.tenant_id != tenant.id:
        raise ValueError("Zuordnungstabelle nicht gefunden.")
    return vault
