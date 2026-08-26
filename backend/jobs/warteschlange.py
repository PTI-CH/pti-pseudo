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

from sqlalchemy import Select, delete, select, update
from sqlalchemy.orm import Session

from backend.adapters import get_adapter
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

#: Statuswerte, die :func:`raeume_ab` unter keinen Umstaenden loescht.
#:
#: Genau einer, und zwar ``laeuft``: Dort und nur dort haelt gerade ein
#: Arbeiter die Zeile in der Hand. Die Begruendung steht bei
#: :func:`raeume_ab`.
UNANTASTBAR = ("laeuft",)


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

    Zwei Pruefungen davor, in dieser Reihenfolge: das **Format** der Datei,
    dann die **Verfuegbarkeit** der Hintergrundverarbeitung. Was an der
    Anfrage falsch ist, geht vor dem, was der Installation fehlt.

    Die Verfuegbarkeitspruefung fragt, ob der Hintergrundarbeiter auf diesem
    Dialekt ueberhaupt laeuft (:func:`hintergrundverarbeitung_moeglich`).
    Ohne sie
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
    # Das Format zuerst — VOR der Frage, ob diese Installation ueberhaupt
    # Auftraege fahren kann.
    #
    # Die Reihenfolge ist nicht beliebig: Eine .pdf auf einer Installation
    # ohne Hintergrundarbeiter bekaeme sonst „bitte teilen Sie die Datei
    # auf" zu lesen — ein Rat, der nichts nuetzt, weil auch die Haelfte
    # einer .pdf keine .pdf weniger ist. Was an der ANFRAGE falsch ist,
    # geht vor dem, was der INSTALLATION fehlt.
    #
    # Ohne diese Pruefung antwortete
    # dieselbe .pdf je nach GROESSE verschieden: unterhalb der Schwelle sofort
    # 415 mit dem Hinweis "bitte als DOCX exportieren", oberhalb erst nach
    # Anlegen, Zuteilen und Ausfuehren als gescheiterter Auftrag -- und die
    # Datei laege bis dahin verschluesselt in der Datenbank. Dieselbe Absage
    # zur selben Zeit ist nicht bloss huebscher, sie ist die einzige, die der
    # Mensch als Formatfehler liest.
    #
    # ``UnsupportedFormatError`` faengt der Router und macht daraus dieselbe
    # 415 wie im synchronen Pfad; die Ausnahme traegt bereits den deutschen
    # Erklaerungstext (``adapters._PLANNED``).
    get_adapter(dateiname)

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
        # Die Verfallszeit gilt AB ANLAGE und nicht erst ab Abschluss.
        #
        # ``melde_fertig``/``melde_fehler`` schieben sie beim Ende ohnehin auf
        # 24 h ab dann. Sie schon hier zu setzen deckt den Fall ab, in dem es
        # nie zu einem Ende kommt: Scheitert der Arbeiterstart im Lifespan --
        # nicht am Dialekt, den faengt ``hintergrundverarbeitung_moeglich``
        # oben, sondern an einem Importfehler oder einer nicht erreichbaren
        # Datenbank --, bliebe eine wartende Zeile sonst OHNE ``expires_at``
        # liegen, und ``raeume_ab`` findet ausschliesslich Zeilen MIT. Die
        # verschluesselte Eingabe laege damit unbegrenzt in der Datenbank.
        expires_at=_jetzt() + AUFBEWAHRUNG,
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
    Rohbytes kennt) plus die Zusatzangaben aus ``AuftragsErgebnis.zusatz``.

    Das ist seit dem 2026-08-26 eine **Obermenge** der synchronen Antwort,
    nicht mehr dieselbe Form. Die gemeinsamen Schluessel (Fundliste,
    angewendete Zahl, Tresor-Kennung, Modus) sind unveraendert die des
    synchronen Pfads; dazu kommen ``requested_entities`` und
    ``ki_pruefung`` — die Einstellungen, mit denen der Lauf gestartet wurde.
    Der synchrone Pfad braucht sie nicht, weil die Oberflaeche sie dort noch
    im Speicher hat; nach einem Seitenneuladen hat sie das nicht, und ohne
    diese beiden Angaben liefe „Anwenden" auf den Vorgabewerten statt auf
    denen des Laufs — im schlimmsten Fall mit einem Tresor, den niemand
    bestellt hat. Die Aufstellung steht in ``API.md``.

    Die
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


def _verwaiste_abfrage(grenze: datetime, ist_postgres: bool) -> Select:
    """Laufende Auftraege jenseits der Verwaisungsfrist, Zeile gesperrt.

    Dieselbe Sperre wie in :func:`_wartende_abfrage`, aus demselben Grund:
    Zwei Worker raeumen gleichzeitig auf, lesen beide dieselbe haengende Zeile
    und zaehlen beide eine Freigabe darauf. Ein Auftrag verbrauchte damit
    seine drei Versuche (:data:`MAX_FREIGABEN`) in der halben Zeit und
    scheiterte endgueltig, obwohl er nur zweimal wirklich unterbrochen wurde.

    ``skip_locked`` heisst, dass der zweite Worker die gesperrte Zeile in
    dieser Runde ueberspringt -- die Rueckgabe von
    :func:`gib_verwaiste_frei` faellt dann eher zu NIEDRIG aus. Das ist die
    richtige Richtung: Der andere Worker gibt sie ja gerade frei, und der
    naechste Aufraeumlauf kommt in rund fuenf Minuten.

    Auf SQLite ohne Sperre — dort gibt es nur einen Schreiber, und
    ``SKIP LOCKED`` kennt der Dialekt gar nicht.
    """
    abfrage = select(Job).where(Job.status == "laeuft", Job.updated_at < grenze)
    if ist_postgres:
        abfrage = abfrage.with_for_update(skip_locked=True)
    return abfrage


def gib_verwaiste_frei(session: Session) -> int:
    """Holt haengengebliebene Auftraege zurueck in die Warteschlange.

    Ein Auftrag, dessen Bearbeiter mitten im Lauf verschwand — Deploy,
    Absturz, Speichermangel —, stuende sonst fuer immer auf ``laeuft``.

    Wer die Grenze :data:`MAX_FREIGABEN` reisst, gilt als gescheitert: Ein
    Dokument, das den Arbeiter zuverlaessig umbringt, soll nicht ewig im Kreis
    wandern und dabei jedes Mal einen Worker mitnehmen.
    """
    grenze = _jetzt() - VERWAIST_NACH
    ist_postgres = session.bind.dialect.name == "postgresql"
    verwaiste = session.scalars(_verwaiste_abfrage(grenze, ist_postgres)).all()

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
    """Entfernt abgelaufene Auftraege samt Ergebnis — **ausser den laufenden**.

    **Unantastbar ist genau ``laeuft``** (:data:`UNANTASTBAR`), und zwar erst
    seit die Verfallszeit schon bei der Anlage gesetzt wird. Vorher trug
    ``expires_at`` nur, wer bereits ``fertig`` oder ``fehler`` war; heute traegt
    es jede Zeile ab Sekunde eins, auch die gerade bearbeitete. Ein Auftrag,
    der 24 h nach seiner Anlage noch laeuft, verloere sonst seine Zeile mitten
    im Lauf, und das anschliessende ``UPDATE`` aus :func:`melde_fertig` wuerfe
    einen ``StaleDataError`` — ein DELETE auf eine verschwundene Zeile warnt
    nur, ein UPDATE wirft.

    **``wartend`` bleibt ausdruecklich loeschbar.** Ein Filter auf
    ``fertig``/``fehler`` waere bequem und falsch: Er naehme genau den Zweck
    zurueck, fuer den die Verfallszeit ab Anlage eingefuehrt wurde — eine
    wartende Zeile aufzuraeumen, die nie jemand abarbeitet, weil der
    Arbeiterstart gescheitert ist. Die traegt die verschluesselte Eingabedatei
    mit sich.

    **Der laufenden Zeile geht dadurch nichts verloren.**
    :func:`gib_verwaiste_frei` laeuft im Aufraeumlauf VOR dieser Funktion und
    hat eine haengengebliebene ``laeuft``-Zeile bis hierher laengst auf
    ``wartend`` oder ``fehler`` gesetzt; spaetestens im uebernaechsten
    Durchgang ist sie damit auch abraeumbar. Unangetastet bleibt nur, was
    tatsaechlich noch bearbeitet wird.

    **Ein Massenloeschen, keine Schleife ueber gelesene Zeilen.** Die fruehere
    Fassung las erst per ``select`` und loeschte dann Zeile fuer Zeile — in dem
    Fenster dazwischen kann ein anderer Worker eine ``wartend``-Zeile ueber
    :func:`hole_naechsten` zugeteilt haben, und geloescht wuerde dann doch eine
    laufende. Hier wertet die Datenbank die Bedingung selbst aus: Das DELETE
    laeuft in dieselbe Zeilensperre wie ``FOR UPDATE`` und prueft ``status``
    danach erneut. ``Job`` haengt an keiner Beziehung (kein ``relationship``,
    keine Fremdschluessel auf ``jobs.id``), es geht also kein ORM-Kaskadieren
    verloren.
    """
    entfernt = session.execute(
        delete(Job).where(
            Job.expires_at.is_not(None),
            Job.expires_at < _jetzt(),
            Job.status.not_in(UNANTASTBAR),
        ),
        # ``fetch`` und ausdruecklich nicht die Vorgabe ``auto``: Die wertet
        # die Bedingung zusaetzlich in Python gegen die bereits geladenen
        # Objekte aus — und SQLite gibt Zeitstempel OHNE Zone zurueck, was
        # den Vergleich gegen das zonenbehaftete ``_jetzt()`` mit
        # ``TypeError: can't compare offset-naive and offset-aware datetimes``
        # beendet (gemessen, nicht vermutet). ``fetch`` laesst die Datenbank
        # entscheiden und holt sich nur die getroffenen Schluessel, um die
        # Sitzung auf Stand zu halten: kein Objekt bleibt als lebend zurueck,
        # das gerade geloescht wurde.
        execution_options={"synchronize_session": "fetch"},
    ).rowcount
    if entfernt:
        logger.info("Abgelaufene Auftraege entfernt: %d", entfernt)
    return entfernt


def aufraeumlauf(session: Session) -> int:
    """Ein vollstaendiger Aufraeumdurchgang. Zahl der entfernten Auftraege.

    Die **einzige** Fassung dieser zwei Schritte im Baum: Der Arbeiterfaden
    ruft sie in jedem 150. Durchgang (:mod:`backend.jobs.arbeiter`), der
    Lifespan einmal beim Prozessstart (``backend/main.py``). Zwei Kopien
    derselben Reihenfolge waeren hier besonders teuer, weil die Reihenfolge
    tragend ist und nicht Geschmack: :func:`raeume_ab` verschont laufende
    Zeilen und verlaesst sich darauf, dass :func:`gib_verwaiste_frei` die
    haengengebliebenen davon vorher zurueckgesetzt hat.

    Sie sitzt hier und nicht in ``backend/main.py``, weil der Arbeiter sie von
    dort nicht importieren koennte, ohne einen Zirkel zu riskieren
    (``main`` -> Router -> ... , und der Lifespan importiert seinerseits den
    Arbeiter). Dieses Modul kennt umgekehrt niemanden von beiden.

    Committet selbst — anders als der Rest dieses Moduls, der nur ``flush``t.
    Ein Aufraeumdurchgang ist eine abgeschlossene Arbeitseinheit ohne
    Fortsetzung beim Aufrufer; beide Aufrufer haben unmittelbar danach ohnehin
    committet.
    """
    gib_verwaiste_frei(session)
    entfernt = raeume_ab(session)
    session.commit()
    return entfernt
