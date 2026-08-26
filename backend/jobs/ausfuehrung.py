"""Die Naht zwischen Auftrag und Pipeline.

Die Warteschlange weiss nichts von Dokumenten, die Pipeline nichts von
Auftraegen. Hier treffen sie sich — und nur hier. Dadurch bleibt beides fuer
sich pruefbar, und ein neuer Auftragstyp (Zyklus B: OCR) beruehrt genau diese
eine Datei.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.core.kennungen import als_uuid
from backend.db import get_sessionmaker
from backend.pipeline import anonymize_document, reverse_document
from backend.vault.models import Job, Tenant, UsageEvent, Vault

logger = logging.getLogger("pti.jobs.ausfuehrung")


class NutzermeldungError(ValueError):
    """Ein Fehler, dessen Text fuer den Menschen in der Oberflaeche bestimmt ist.

    Der Arbeiter schreibt ``job.fehler``, und die Oberflaeche zeigt das Feld
    unveraendert an. Jede Ausnahme dort ungefiltert hineinzuschreiben hiesse,
    einem Aufrufer den Wortlaut beliebiger Bibliotheken zu zeigen -- heute
    harmlos (nachgeprueft: lxml, openpyxl und python-docx melden auf diesem
    Weg nur Formatfehler ohne Dokumentinhalt), aber niemand kontrolliert
    diese Texte, und mit dem OCR-Zyklus kommen Bibliotheken dazu, die
    Dateipfade und Ausschnitte in ihre Meldungen schreiben.

    Diese Klasse ist die Erlaubnis. Wer sie wirft, hat den Text bewusst
    formuliert; alles andere bekommt vom Arbeiter eine allgemeine Meldung,
    und der Wortlaut steht nur im Protokoll (siehe
    ``arbeiter._menschliche_meldung``).
    """


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
    Modus, Zeichenzahl, KI-Status sowie die Kategorienwahl und das KI-Haekchen
    des Laufs; ``reverse`` nur die Tresor-Kennung. Ein Tupel mit fester
    Stellenzahl haette diesen Unterschied nicht sauber abgebildet.

    Mit den letzten beiden Angaben ist der Umschlag eine **Obermenge** der
    synchronen Antwort, nicht mehr deckungsgleich mit ihr. Das ist Absicht:
    Der synchrone Aufrufer hat seine Einstellungen noch, der wiederaufnehmende
    nicht — er kennt nur die Auftragskennung.
    """

    daten: bytes
    zusatz: dict = field(default_factory=dict)


def _erfasse_nutzung(
    session: Session, job: Job, *, action: str, entity_counts, char_count: int
) -> None:
    """Schreibt den Nutzungseintrag eines Auftrags.

    **Warum es das braucht.** Die Auswertung ``GET /api/v1/api-keys/usage``
    zaehlt ``usage_events``. Der synchrone Pfad schreibt dort seit jeher
    (``documents._record_usage``), der Auftragspfad nicht -- ausgerechnet die
    grossen Laeufe fehlten damit in jeder Auswertung, und zwar unsichtbar:
    Die Zahlen sahen vollstaendig aus, waren aber systematisch zu klein, und
    der Fehler waechst mit der Dateigroesse.

    **Dieselben Aktionsnamen wie synchron** (``anonymize_file``,
    ``reverse_file``), nicht eigene. Ob eine Datei ueber der Schwelle lag, ist
    eine Frage des Transportwegs und nicht der Nutzung; eigene Namen wuerden
    dieselbe Taetigkeit in der Auswertung auf zwei Zeilen aufteilen, die
    niemand wieder zusammenrechnet.

    **Der Aktor kommt aus ``job.aktor``**, nicht aus einem Request -- der
    laeuft laengst nicht mehr. ``core.deps.current_username`` gibt genau
    denselben Wert zurueck wie ``current_actor``, den ``lege_an``
    gespeichert hat; Menschen stehen dort mit ihrem Hub-Benutzernamen,
    Maschinen als ``apikey:<id>``.

    Wie im synchronen Pfad: ausschliesslich Metadaten und Zahlen, nie
    Inhalte, nie Dateinamen (Fachkonzept Abschnitt 9).
    """
    session.add(
        UsageEvent(
            tenant_id=job.tenant_id,
            username=job.aktor or "unbekannt",
            action=action,
            entity_counts=entity_counts,
            char_count=char_count,
        )
    )


def fuehre_aus(
    job: Job, daten: bytes, melde_fortschritt: Callable[[int, int], None]
) -> AuftragsErgebnis:
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
                raise NutzermeldungError(
                    "Für die Rückführung wird eine vault_id benötigt."
                )
            vault = _lade_bestehenden_vault(session, tenant, vault_id)
            ergebnis = reverse_document(session, vault, daten, dateiname)
            # Dieselben Werte wie ``documents.reverse_file``: keine
            # Fundzaehlung (die Rueckfuehrung erkennt nichts neu), als
            # Umfangsmass die Bytezahl der Eingabe.
            _erfasse_nutzung(
                session, job, action="reverse_file",
                entity_counts=None, char_count=len(daten),
            )
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
            _erfasse_nutzung(
                session, job, action="anonymize_file",
                entity_counts=befund.counts_by_type(),
                char_count=befund.char_count,
            )
            session.commit()
            # Dieselben Schluessel wie die Antwort des synchronen Pfads
            # (``documents.anonymize_file``) — die Ergebnisroute uebernimmt
            # sie woertlich. Die letzten beiden hat der synchrone Pfad NICHT:
            # Der Umschlag ist eine Obermenge seiner Antwort (siehe
            # ``routers/jobs.py:ergebnis`` und API.md). Sie beantworten die
            # Frage, die sich nur beim Auftrag stellt: Womit lief dieser Lauf?
            # Wer die Seite neu laedt und den Auftrag wiederaufnimmt, hat
            # seine Bedienelemente von damals nicht mehr — ohne diese beiden
            # Angaben faellt die Oberflaeche beim "Anwenden" auf ihre
            # Vorgabewerte zurueck und legte fuer einen ``anonymous``-Lauf
            # einen Tresor an, den niemand bestellt hat.
            zusatz = {
                "entities": [f.as_dict() for f in befund.findings],
                "applied": len(gewaehlt),
                "vault_id": str(vault.id) if vault else None,
                "mode": parameter.get("modus"),
                "char_count": befund.char_count,
                "ki_geprueft": befund.ki_geprueft,
                # Nicht ``entities``: der Schluessel traegt im Umschlag schon
                # die FUNDLISTE. ``requested_entities`` sagt, wonach gesucht
                # wurde, ``entities``, was gefunden wurde — zwei Dinge, die
                # sich unter einem Namen still gegenseitig ueberschrieben
                # haetten. ``None`` heisst wie ueberall: keine ausdrueckliche
                # Wahl, es galt die Vorgabe des Servers.
                "requested_entities": parameter.get("entities"),
                # Angefordert, nicht gelaufen — ``ki_geprueft`` daneben sagt,
                # ob die Pruefung tatsaechlich stattfand. Die Oberflaeche
                # braucht beides: das Haekchen fuer den naechsten Aufruf, den
                # Status fuer die Warnung "nur lokal geprueft".
                "ki_pruefung": bool(parameter.get("ki_pruefung")),
            }
            return AuftragsErgebnis(daten=ergebnis, zusatz=zusatz)

    raise NutzermeldungError(f"Unbekannter Auftragstyp: {job.mode}")


def _hole_vault(session: Session, tenant: Tenant, parameter: dict) -> Optional[Vault]:
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
        raise NutzermeldungError(
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


def _lade_bestehenden_vault(session: Session, tenant: Tenant, vault_id: str) -> Vault:
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
    dieselbe Umwandlung wie ``documents._as_uuid`` fuer den synchronen Pfad
    (beide nutzen :func:`backend.core.kennungen.als_uuid`), nur mit
    ``NutzermeldungError`` statt ``HTTPException`` — ein Auftrag laeuft nicht
    in einer Anfrage, und der ``Arbeiter`` faengt ``ValueError`` und setzt
    den Auftrag auf ``fehler``; die Meldung landet fuer den Menschen sichtbar
    in ``job.fehler``.
    """
    vault_uuid = als_uuid(vault_id)
    if vault_uuid is None:
        raise NutzermeldungError("Ungültige Zuordnungstabellen-Kennung.")
    vault = session.get(Vault, vault_uuid)
    if vault is None or vault.tenant_id != tenant.id:
        raise NutzermeldungError("Zuordnungstabelle nicht gefunden.")
    return vault
