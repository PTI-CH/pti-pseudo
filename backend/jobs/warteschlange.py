"""Die Auftragstabelle als Warteschlange — reine Datenbanklogik.

Bewusst ohne Faeden und ohne HTTP: Was mit der Datenbank geschieht, ist hier
und ist ohne Nebenlaeufigkeit pruefbar. Wann es geschieht, steht in
:mod:`backend.jobs.arbeiter`.

**Die Zuteilung ist der heikle Teil.** Zwei uvicorn-Worker greifen nach
derselben Zeile. Waehlte jeder Prozess fuer sich aus, bekaemen beide denselben
Auftrag: Das Dokument wuerde doppelt verarbeitet, und der Vault vergaebe zwei
Pseudonyme fuer denselben Namen. Entschieden wird deshalb in der Datenbank.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from backend.jobs.ausfuehrung import AuftragsErgebnis
from backend.vault import crypto
from backend.vault.models import Job

logger = logging.getLogger("pti.jobs")

#: Wie lange ein fertiges Ergebnis abholbar bleibt.
#:
#: Vierundzwanzig Stunden decken „ich mache morgen frueh weiter" ab, ohne dass
#: sich Dokumente ueber Wochen sammeln.
AUFBEWAHRUNG = timedelta(hours=24)

#: Ab wann ein laufender Auftrag als verwaist gilt.
#:
#: Grosszuegig gegenueber einem langsamen Lauf (Zyklus B rechnet mit rund zwei
#: Sekunden je gescannter Seite) und knapp genug, dass niemand ewig wartet.
VERWAIST_NACH = timedelta(minutes=10)

#: Nach so vielen Freigaben gilt ein Auftrag als endgueltig gescheitert.
MAX_FREIGABEN = 3


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def hintergrundverarbeitung_moeglich(dialect_name: str) -> bool:
    """Ob der Auftragsarbeiter auf diesem Datenbank-Dialekt ueberhaupt laeuft.

    Dieselbe Bedingung wie beim Arbeiterstart im Lifespan (``backend/main.py``)
    -- als eigene Funktion, damit Start und Anlegen sie gemeinsam benutzen und
    nicht zwei Fassungen derselben Bedingung auseinanderlaufen. Der Grund fuer
    die Beschraenkung auf PostgreSQL steht bei :func:`hole_naechsten`: SQLite
    teilt sich im Testbetrieb (und in der Ein-Knoten-Rueckfallvariante, siehe
    ``JELASTIC_SETUP.md`` 1b) eine einzige ``StaticPool``-Verbindung, die ein
    daneben pollender Hintergrundfaden zerlegen wuerde.
    """
    return dialect_name == "postgresql"


class AuftragNichtMoeglichError(RuntimeError):
    """Ein Auftrag wurde angefordert, den nie jemand abarbeiten wuerde.

    Ausdruecklich keine ``HTTPException``: Dieses Modul bleibt frei von HTTP
    (siehe Modul-Docstring). Die Uebersetzung in eine Statuszeile (503)
    geschieht im Router, der :func:`lege_an` aufruft.
    """


def _ohne_klartext(parameter: dict) -> dict:
    """Streift Klartext aus der Fundauswahl, bevor er in die Datenbank geht.

    ``selected_entities`` traegt von den Aufrufern her historisch auch
    ``text`` (den im Original gefundenen Klartext) und ``score`` mit --
    beides reine Anzeige-Beigabe fuer die Befundliste der Oberflaeche.
    ``pipeline._select`` liest davon nichts: ein Fund wird ausschliesslich
    ueber das Tripel ``(segment_id, start, end)`` wiedererkannt. Ungefiltert
    durchgereicht, laege die vollstaendige Fundliste eines Dokuments -- Namen,
    AHV-Nummern, IBANs -- als lesbarer Text in ``parameter_json``, einer
    normalen, unverschluesselten JSONB-Spalte, sichtbar in jedem
    Datenbankabzug und jeder Sicherung.

    Sitzt bewusst hier und nicht im Router: ``lege_an`` ist die einzige
    Stelle im ganzen Baum, an der ueberhaupt eine Auftragszeile entsteht --
    ein Aufrufer ueber ``/api/ext/v1`` kaeme an einer Pruefung im Router
    vorbei, an dieser hier nicht.
    """
    auswahl = parameter.get("selected_entities")
    if not isinstance(auswahl, list):
        return parameter
    bereinigt = dict(parameter)
    bereinigt["selected_entities"] = [
        {
            "segment_id": e.get("segment_id"),
            "start": e.get("start"),
            "end": e.get("end"),
            "entity_type": e.get("entity_type"),
        }
        for e in auswahl
        if isinstance(e, dict)
    ]
    return bereinigt


def lege_an(session: Session, *, tenant, aktor: str, mode: str,
            dateiname: str, daten: bytes, parameter: dict) -> Job:
    """Legt einen wartenden Auftrag an und versiegelt die Eingabe.

    Prueft zuerst, ob der Hintergrundarbeiter auf diesem Dialekt ueberhaupt
    laeuft (:func:`hintergrundverarbeitung_moeglich`). Ohne diese Pruefung
    bekaeme eine grosse Datei eine Auftragszeile, die nie jemand abarbeitet:
    Der Auftrag bliebe fuer immer auf ``wartend`` stehen, und die Oberflaeche
    fragte endlos nach, ohne dass irgendwo stuende, warum -- genau das
    Fehlerbild, gegen das der Arbeiterstart in ``main.py`` schon einmal
    entschieden wurde. Die Pruefung sitzt bewusst HIER und nicht erst im
    Router: ``lege_an`` ist die einzige Stelle im ganzen Baum, an der ueber-
    haupt eine Auftragszeile entsteht -- ein kuenftiger zweiter Aufrufer
    (eine weitere Route, ein Skript) wuerde eine Pruefung im Router sonst
    versehentlich umgehen koennen, eine Pruefung hier nicht.
    """
    if not hintergrundverarbeitung_moeglich(session.bind.dialect.name):
        raise AuftragNichtMoeglichError(
            "Grosse Dateien werden im Hintergrund verarbeitet; das ist auf "
            "dieser Installation nicht eingerichtet. Bitte teilen Sie die "
            "Datei auf, oder wenden Sie sich an die Administration."
        )
    job = Job(
        tenant_id=tenant.id,
        mode=mode,
        status="wartend",
        aktor=aktor,
        dateiname=dateiname,
        parameter_json=_ohne_klartext(parameter),
        eingabe_blob=crypto.seal_bytes(str(tenant.id), daten),
        updated_at=_jetzt(),
    )
    session.add(job)
    session.flush()
    logger.info("Auftrag angelegt: %s, %d Byte", job.mode, len(daten))
    return job


def _wartende_abfrage() -> Select:
    """Der naechste wartende Auftrag, PostgreSQL-Zeile gegen Wettlauf gesperrt.

    Genau diese eine Definition benutzt der PostgreSQL-Zweig von
    ``hole_naechsten`` **und** der Test
    ``test_zuteilung_sperrt_die_zeile_und_ueberspringt_gesperrte`` — eine
    nachgebaute Kopie im Test wuerde nur ihre eigene Zeile pruefen und die
    eigentliche Regression (``skip_locked`` oder ``with_for_update`` entfernt)
    nicht bemerken.
    """
    return (
        select(Job)
        .where(Job.status == "wartend")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def hole_naechsten(session: Session) -> Job | None:
    """Teilt genau einen wartenden Auftrag zu — oder gibt ``None`` zurueck.

    Zwei Wege, weil die Testsuite auf SQLite laeuft und PostgreSQL in
    Produktion:

    * **PostgreSQL:** ``FOR UPDATE SKIP LOCKED`` (siehe
      :func:`_wartende_abfrage`) — der zweite Worker ueberspringt die
      gesperrte Zeile, statt auf sie zu warten.
    * **SQLite:** ein bedingtes ``UPDATE ... WHERE status = 'wartend'``. Die
      Zahl der betroffenen Zeilen sagt, wer gewonnen hat. SQLite kennt
      ``SKIP LOCKED`` nicht und hat ohnehin nur einen Schreiber.

    Warum es fuer die Zuteilung keinen fadenbasierten Test gibt: Die
    Testsuite laeuft auf SQLite mit ``StaticPool``, der fuer alle Sitzungen
    dieselbe zugrunde liegende DBAPI-Verbindung zurueckgibt. Zwei echte
    Faeden, die gleichzeitig auf diese eine Verbindung zugreifen, ergeben
    ``sqlite3.InterfaceError: bad parameter or other API misuse`` — am
    2026-08-25 gemessen: 4 von 5 Laeufen. Selbst ein zuverlaessiger
    Fadentest bewachte hier nur den SQLite-Zweig, der in Produktion nie
    laeuft — die Regression, die tatsaechlich weh taete (``skip_locked``
    verschwindet aus dem PostgreSQL-Zweig), koennte SQLite so oder so nie
    bemerken. Deshalb prueft :func:`_wartende_abfrage` stattdessen die
    kompilierte SQL gegen den PostgreSQL-Dialekt, ohne eine Datenbank zu
    brauchen.
    """
    ist_postgres = session.bind.dialect.name == "postgresql"

    if ist_postgres:
        job = session.scalars(_wartende_abfrage()).first()
        if job is None:
            return None
        job.status = "laeuft"
        job.updated_at = _jetzt()
        session.flush()
        return job

    kandidat = session.scalars(
        select(Job).where(Job.status == "wartend").order_by(Job.created_at).limit(1)
    ).first()
    if kandidat is None:
        return None
    betroffen = session.execute(
        update(Job)
        .where(Job.id == kandidat.id, Job.status == "wartend")
        .values(status="laeuft", updated_at=_jetzt())
    ).rowcount
    if betroffen != 1:
        return None
    session.flush()
    session.refresh(kandidat)
    return kandidat


def melde_fertig(session: Session, job: Job, ergebnis: AuftragsErgebnis) -> None:
    """Schliesst den Auftrag ab — und **loescht die Eingabe**.

    ``ergebnis_blob`` traegt seit Aufgabe 9 keine nackten Dokumentbytes mehr,
    sondern einen JSON-Umschlag: Dokument (als Base64, weil JSON keine
    Rohbytes kennt) plus dieselben Zusatzangaben, die der synchrone Pfad in
    seiner Antwort liefert — Fundliste, angewendete Zahl, Tresor-Kennung. Die
    Fundliste enthaelt Klartext-Namen, gehoert also genau dorthin und
    nirgendwo sonst; das ist unbedenklich, weil der ganze Umschlag wie zuvor
    AES-GCM-verschluesselt wird, nur eben groesser als vorher. Ohne die
    Fundliste koennte der Mensch bei einem Auftrag keinen Fehlalarm abwaehlen,
    ohne die Tresor-Kennung waere ein umkehrbar verarbeitetes Dokument nie
    wieder rueckfuehrbar.
    """
    umschlag = {
        "content_base64": base64.b64encode(ergebnis.daten).decode("ascii"),
        **ergebnis.zusatz,
    }
    job.status = "fertig"
    job.ergebnis_blob = crypto.seal(str(job.tenant_id), json.dumps(umschlag))
    job.eingabe_blob = None
    job.expires_at = _jetzt() + AUFBEWAHRUNG
    job.updated_at = _jetzt()
    session.flush()
    logger.info(
        "Auftrag fertig: %s, Ergebnis %d Byte", job.mode, len(ergebnis.daten)
    )


def melde_fehler(session: Session, job: Job, meldung: str) -> None:
    """Markiert den Auftrag als gescheitert — und loescht die Eingabe trotzdem.

    Auch ein gescheiterter Auftrag darf keine Personendaten liegenlassen. Das
    ist eine eigene Zusicherung mit eigenem Test, kein Nebeneffekt.
    """
    job.status = "fehler"
    job.fehler = meldung
    job.eingabe_blob = None
    job.expires_at = _jetzt() + AUFBEWAHRUNG
    job.updated_at = _jetzt()
    session.flush()
    logger.info("Auftrag gescheitert: %s", job.mode)


def schlage_herz(session: Session, job_id) -> None:
    """Frischt ``updated_at`` eines laufenden Auftrags auf, ohne den Fortschritt.

    Gegenstueck zu ``melde_fortschritt`` (siehe ``backend/jobs/arbeiter.py``)
    fuer Formate, die keinen Fortschritt melden -- heute alle. Ohne diesen
    Takt friert ``updated_at`` auf den Zeitpunkt der Zuteilung ein, und
    :func:`gib_verwaiste_frei` erklaert jeden gesunden Lauf, der laenger als
    ``VERWAIST_NACH`` dauert, faelschlich fuer verwaist -- ein zweiter Worker
    faengt dasselbe Dokument dann von vorn an, gegen denselben Tresor.

    Die Bedingung ``status == "laeuft"`` ist eine Vorsichtsmassnahme gegen
    eine seltene Ueberschneidung: Ist der Auftrag zwischen dem letzten Takt
    und diesem Aufruf bereits fertig oder gescheitert, bewirkt dieser Aufruf
    dann einfach nichts, statt versehentlich einen abgeschlossenen Auftrag
    anzufassen.
    """
    session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "laeuft")
        .values(updated_at=_jetzt())
    )


def gib_verwaiste_frei(session: Session) -> int:
    """Holt haengengebliebene Auftraege zurueck in die Warteschlange.

    Ein Auftrag, dessen Bearbeiter mitten im Lauf verschwand — Deploy,
    Absturz, Speichermangel —, stuende sonst fuer immer auf ``laeuft``.

    Wer die Grenze :data:`MAX_FREIGABEN` reisst, gilt als gescheitert: Ein
    Dokument, das den Arbeiter zuverlaessig umbringt, soll nicht ewig im Kreis
    wandern und dabei jedes Mal einen Worker mitnehmen.
    """
    grenze = _jetzt() - VERWAIST_NACH
    verwaiste = session.scalars(
        select(Job).where(Job.status == "laeuft", Job.updated_at < grenze)
    ).all()

    for job in verwaiste:
        job.freigaben = (job.freigaben or 0) + 1
        if job.freigaben >= MAX_FREIGABEN:
            melde_fehler(
                session, job,
                "Die Verarbeitung wurde mehrfach unterbrochen und aufgegeben. "
                "Bitte laden Sie die Datei erneut hoch.",
            )
        else:
            job.status = "wartend"
            job.updated_at = _jetzt()

    if verwaiste:
        session.flush()
        logger.info("Verwaiste Auftraege freigegeben: %d", len(verwaiste))
    return len(verwaiste)


def raeume_ab(session: Session) -> int:
    """Entfernt abgelaufene Auftraege samt Ergebnis."""
    abgelaufen = session.scalars(
        select(Job).where(Job.expires_at.is_not(None), Job.expires_at < _jetzt())
    ).all()
    for job in abgelaufen:
        session.delete(job)
    if abgelaufen:
        session.flush()
        logger.info("Abgelaufene Auftraege entfernt: %d", len(abgelaufen))
    return len(abgelaufen)
