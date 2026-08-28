"""Der Hintergrundfaden, der die Warteschlange abarbeitet.

**Warum ein Faden und kein eigener Prozess:** Der Jelastic-Runner startet genau
einen Befehl, und der Benutzer auf dem Knoten hat kein ``sudo`` — wir koennen
dort keinen zweiten Dienst einrichten. Der Arbeiter muss also in der Anwendung
leben. Je uvicorn-Worker einer; die Zuteilung regelt die Datenbank
(:mod:`backend.jobs.warteschlange`).

**Ein Auftrag je Worker gleichzeitig.** Nicht wegen der Datenbank, sondern wegen
des Speichers: Zyklus B (OCR) misst 1.1 GB Spitze je Lauf. Die Grenze gehoert
in den Code und nicht in die Knotenkonfiguration — eine Schleife, die Speicher
frisst, soll an unserer Grenze scheitern und nicht auf der Rechnung erscheinen.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from backend.adapters import UnsupportedFormatError
from backend.jobs import warteschlange
from backend.jobs.ausfuehrung import NutzermeldungError
from backend.vault import crypto

logger = logging.getLogger("pti.jobs.arbeiter")

#: Abstand zwischen zwei Blicken in die Warteschlange.
TAKT_SEKUNDEN = 2.0

#: Abstand zwischen zwei Aufraeumlaeufen. Abgelaufene Auftraege haben es nicht
#: eilig, und eine zusaetzliche Abfrage alle zwei Sekunden waere Verschwendung.
#:
#: **Eine Zeitspanne und keine Durchgangszahl**, seit dem 2026-08-28. Vorher
#: raeumte jeder 150. Durchgang auf, mit „rund alle fuenf Minuten" daneben --
#: was nur im Leerlauf stimmte. Ein Durchgang wartet naemlich
#: :data:`TAKT_SEKUNDEN` **nur, wenn er nichts gefunden hat** (siehe
#: ``_schleife``): Wer eine volle Warteschlange abarbeitet, zaehlt 150
#: Durchgaenge in der Zeit, die 150 Auftraege brauchen -- und raeumt damit
#: ausgerechnet unter Last am oeftesten auf, wo die Abfrage am teuersten ist
#: und der Nutzen am kleinsten. Die Uhr entkoppelt den Aufraeumtakt von der
#: Last: fuenf Minuten sind fuenf Minuten, ob Auftraege laufen oder nicht.
AUFRAEUMEN_ALLE = timedelta(minutes=5)

#: Mindestabstand zwischen zwei geschriebenen Fortschrittsmeldungen.
#:
#: Der Rueckruf aus der Pipeline kommt je Segment -- bei einer grossen Tabelle
#: zehntausendfach, und jede Meldung waere sonst ein eigenes ``UPDATE`` samt
#: ``COMMIT``. Gedrosselt wird HIER und nicht in der Pipeline: Wer den
#: Rueckruf billig beantwortet, soll ihn vollstaendig bekommen; teuer ist er
#: nur fuer uns.
#:
#: Zwei Sekunden, weil die Oberflaeche in genau diesem Takt nachfragt
#: (``AUFTRAG_TAKT_MS`` in ``frontend/src/core/api.ts``) -- oefter zu
#: schreiben, als jemand liest, brauchte niemand.
FORTSCHRITT_TAKT_SEKUNDEN = 2.0

#: Abstand zwischen zwei Herzschlaegen waehrend eine Verarbeitung laeuft.
#:
#: Deutlich unter ``warteschlange.VERWAIST_NACH`` (10 Minuten), damit ein
#: gesunder, lang laufender Auftrag garantiert mehrfach aufgefrischt wird,
#: bevor die Verwaisungsfrist reisst -- kein einziger verpasster Takt darf
#: ausreichen, um einen laufenden Auftrag faelschlich zu verwaisen.
HERZSCHLAG_SEKUNDEN = 60.0


#: Ausnahmen, deren Wortlaut in ``job.fehler`` und damit vor die Augen des
#: Nutzers darf. Beide tragen bewusst formulierte deutsche Texte:
#: :class:`~backend.jobs.ausfuehrung.NutzermeldungError` von uns selbst,
#: ``UnsupportedFormatError`` aus dem Formatregister
#: (``adapters._PLANNED``, mit Hinweis auf den Ausweg).
DURCHREICHBAR = (NutzermeldungError, UnsupportedFormatError)

#: Was der Nutzer stattdessen liest, wenn eine andere Ausnahme hochkam.
ALLGEMEINE_MELDUNG = (
    "Die Datei konnte nicht verarbeitet werden. Bitte prüfen Sie, ob sie "
    "sich in Ihrem Programm öffnen lässt, und wenden Sie sich sonst an die "
    "Administration."
)


def _menschliche_meldung(fehler: BaseException) -> str:
    """Was von einer Ausnahme in ``job.fehler`` stehen darf.

    ``job.fehler`` geht unveraendert in die Oberflaeche. Bis hierher stand
    dort ``str(fehler)`` jeder beliebigen Ausnahme -- der Wortlaut von
    lxml, openpyxl, python-docx und kuenftig der OCR-Bibliotheken.

    **Heute ist das nachgeprueft harmlos**: Die genannten Bibliotheken melden
    auf diesem Weg Formatfehler ohne Dokumentinhalt. Aber niemand
    kontrolliert diese Texte, sie aendern sich mit jeder Version, und der
    OCR-Zyklus bringt Bibliotheken mit, die Dateipfade und erkannte
    Textausschnitte in ihre Meldungen schreiben. Eine Meldung wie
    ``cannot decode 'Muster, Hans' at offset 41`` stuende dann in einem Feld,
    das die Oberflaeche anzeigt und die externe API zurueckgibt -- und der
    ganze Auftragspfad existiert, um genau solche Namen NICHT herauszugeben.

    Die Umkehrung ist bewusst: Durchreichen ist die Ausnahme und muss
    beantragt werden (:data:`DURCHREICHBAR`), nicht das Filtern. Eine neue
    Bibliothek ist damit ab dem ersten Tag stumm, statt bis zu ihrer ersten
    auffaelligen Meldung zu reden.

    Der vollstaendige Wortlaut geht nicht verloren -- er steht mit
    ``exc_info=True`` im Protokoll, wo ihn der Betreiber sieht und der
    Aufrufer nicht.
    """
    if isinstance(fehler, DURCHREICHBAR):
        return str(fehler)
    return ALLGEMEINE_MELDUNG


class Arbeiter:
    """Holt Auftraege und fuehrt sie aus. Ein Faden je Prozess."""

    def __init__(self, session_factory, ausfuehren: Callable) -> None:
        self._session_factory = session_factory
        #: ``(job, daten: bytes, melde_fortschritt) -> AuftragsErgebnis``
        self._ausfuehren = ausfuehren
        self._stopp = threading.Event()
        self._faden: threading.Thread | None = None
        #: Wann zuletzt aufgeraeumt wurde, auf der **monotonen** Uhr: Eine
        #: Zeitumstellung oder ein NTP-Sprung darf den Aufraeumtakt nicht
        #: aussetzen lassen oder verdoppeln.
        #:
        #: Startwert „jetzt" und nicht „nie", damit der erste Durchgang NICHT
        #: aufraeumt: Der Lifespan hat das beim Prozessstart gerade getan
        #: (``backend/main.py``, vor und unabhaengig vom Arbeiterstart) --
        #: derselbe Lauf zwei Sekunden spaeter noch einmal findet
        #: zuverlaessig nichts.
        self._zuletzt_aufgeraeumt = time.monotonic()

    def _herzschlag_starten(self, job_id) -> Callable[[], None]:
        """Startet einen Faden, der ``updated_at`` periodisch auffrischt.

        Ohne einen nebenlaeufigen Takt gaebe es waehrend eines langen Laufs
        keine Gelegenheit, ``updated_at`` aufzufrischen, und
        :func:`warteschlange.gib_verwaiste_frei` erklaerte jeden Auftrag ueber
        zehn Minuten faelschlich fuer verwaist -- ein zweiter Worker finge
        dasselbe Dokument dann von vorn an, gegen denselben Tresor.

        **Seit der Segmentzaehlung (2026-08-28) ist er nicht ueberfluessig
        geworden**, obwohl ``melde_fortschritt`` jetzt tatsaechlich gerufen
        wird und dabei ebenfalls ``updated_at`` schreibt. Der Rueckruf deckt
        naemlich nur den Segmentdurchlauf ab. Stumm bleiben: die KI-Schicht,
        die Uebertragung, das Zurueckschreiben und das Metadaten-Streichen --
        zusammen der ganze Rest des Laufs, und bei einem grossen Dokument
        durchaus mehr als zehn Minuten. Ein Auftrag darf nicht deshalb als
        verwaist gelten, weil er die Zaehlphase hinter sich hat.

        Eigene Sitzung je Takt (``self._session_factory()``), nicht die des
        aufrufenden Durchgangs: Diese laeuft in einem anderen Faden waehrend
        ``self._ausfuehren`` blockiert, und eine SQLAlchemy-``Session`` ist
        nicht fadensicher.

        Gibt eine Funktion zurueck, die den Takt stoppt und auf das Ende des
        Fadens wartet -- vom Aufrufer immer in einem ``finally`` zu rufen,
        auch wenn die Verarbeitung mit einer Ausnahme abbricht.
        """
        stopp = threading.Event()

        def takt() -> None:
            while not stopp.wait(HERZSCHLAG_SEKUNDEN):
                try:
                    with self._session_factory() as session:
                        warteschlange.schlage_herz(session, job_id)
                        session.commit()
                except Exception:
                    # Ein verpasster Takt ist kein Grund, den ganzen
                    # Herzschlag zu beenden -- der naechste Takt versucht es
                    # wieder.
                    logger.warning("Herzschlag fehlgeschlagen", exc_info=True)

        faden = threading.Thread(
            target=takt, name="pti-auftraege-herzschlag", daemon=True
        )
        faden.start()

        def stoppen() -> None:
            stopp.set()
            faden.join(timeout=5.0)

        return stoppen

    # ── Der testbare Kern ────────────────────────────────────────────────
    def _ein_durchgang(self) -> bool:
        """Verarbeitet hoechstens einen Auftrag. ``True``, wenn einer lief.

        Die Schleife ist absichtlich NICHT Teil davon: Ein Test, der einen
        Faden startet und auf ihn wartet, wird irgendwann grundlos rot.
        """
        with self._session_factory() as session:
            if (
                time.monotonic() - self._zuletzt_aufgeraeumt
                >= AUFRAEUMEN_ALLE.total_seconds()
            ):
                self._zuletzt_aufgeraeumt = time.monotonic()
                # Dieselbe Funktion, die der Lifespan beim Prozessstart ruft
                # (``backend/main.py``) — bewusst keine zweite Fassung
                # derselben zwei Schritte hier: Die Reihenfolge ist tragend
                # (``raeume_ab`` verschont laufende Zeilen und verlaesst sich
                # darauf, dass ``gib_verwaiste_frei`` die haengengebliebenen
                # vorher zurueckgesetzt hat), und eine Kopie liefe genau
                # daran irgendwann auseinander.
                #
                # Die Uhr wird VOR dem Lauf gestellt, nicht danach: Wirft er,
                # faengt ihn ``_schleife``, und der naechste Versuch soll in
                # fuenf Minuten kommen und nicht im Zwei-Sekunden-Takt.
                warteschlange.aufraeumlauf(session)

            job = warteschlange.hole_naechsten(session)
            if job is None:
                return False
            session.commit()

            zuletzt_gemeldet = 0.0

            def melde_fortschritt(erledigt: int, gesamt: int) -> None:
                """Schreibt den Fortschritt — gedrosselt, aber nie den letzten.

                Der Rueckruf kommt je Segment (``pipeline.detect_in_segments``);
                jede Meldung zu schreiben hiesse, eine grosse Tabelle mit
                zehntausend ``UPDATE … COMMIT`` zu begleiten, waehrend die
                Oberflaeche ohnehin nur alle zwei Sekunden nachfragt.

                ``erledigt >= gesamt`` geht immer durch: Sonst endete der
                Zaehler auf einem beliebigen Zwischenstand, und in der
                Oberflaeche stuende bis zum Statuswechsel „Abschnitt 9'860 von
                10'000" — als haenge der Lauf kurz vor dem Ziel.
                """
                nonlocal zuletzt_gemeldet
                jetzt = time.monotonic()
                if (
                    erledigt < gesamt
                    and jetzt - zuletzt_gemeldet < FORTSCHRITT_TAKT_SEKUNDEN
                ):
                    return
                zuletzt_gemeldet = jetzt
                job.fortschritt = erledigt
                job.gesamt = gesamt
                job.updated_at = datetime.now(timezone.utc)
                session.commit()

            # Der Herzschlag umschliesst AUSSCHLIESSLICH die Verarbeitung und
            # endet, bevor hier irgendetwas an der Auftragszeile geschrieben
            # wird. Das ist kein Aufraeumen aus Ordnungsliebe, sondern der
            # Ausweg aus einem Sperrenkonflikt gegen uns selbst: Schriebe
            # ``melde_fertig`` die Zeile noch waehrend der Takt laeuft, haelte
            # diese Sitzung die Zeilensperre bis zum ``session.commit()``
            # unten -- der Herzschlag-Faden liefe in seiner EIGENEN Sitzung in
            # genau diese Sperre, und ``stoppen()`` wartete die vollen fuenf
            # Sekunden seines ``join`` auf einen Faden, der auf uns wartet.
            # Nicht gelegentlich, sondern bei jedem Auftrag, dessen Takt
            # gerade faellt.
            herzschlag_stopp = self._herzschlag_starten(job.id)
            gescheitert: Exception | None = None
            ergebnis = None
            try:
                # Absichtlich INNERHALB des geschuetzten Bereichs: ein
                # beschaedigter Blob oder ein gewechselter Hauptschlüssel
                # wirft hier, und der Auftrag soll dann sofort mit
                # verstaendlicher Meldung scheitern -- nicht erst nach drei
                # Verwaisungs-Zyklen mit der irrefuehrenden Meldung "mehrfach
                # unterbrochen".
                daten = crypto.open_bytes(str(job.tenant_id), job.eingabe_blob)
                ergebnis = self._ausfuehren(job, daten, melde_fortschritt)
            except Exception as fehler:
                # Jede Ausnahme, nicht nur die erwarteten: Ein Absturz in der
                # Verarbeitung darf den Arbeiter nicht mitnehmen — sonst steht
                # die Warteschlange still, und niemand merkt es. Der volle
                # Wortlaut geht ins Protokoll; in ``job.fehler`` kommt nur,
                # was :func:`_menschliche_meldung` durchlaesst.
                logger.warning("Auftrag gescheitert", exc_info=True)
                gescheitert = fehler
            finally:
                # Muss auch bei einer Ausnahme enden -- sonst tickt der
                # Herzschlag-Faden gegen einen laengst abgeschlossenen
                # Auftrag weiter.
                herzschlag_stopp()

            if gescheitert is not None:
                warteschlange.melde_fehler(
                    session, job, _menschliche_meldung(gescheitert)
                )
            else:
                warteschlange.melde_fertig(session, job, ergebnis)
            session.commit()
            return True

    # ── Schleife und Lebenszyklus ────────────────────────────────────────
    def _schleife(self) -> None:
        while not self._stopp.is_set():
            try:
                gearbeitet = self._ein_durchgang()
            except Exception:
                # Auch ein Fehler in der Warteschlange selbst (Datenbank weg)
                # darf den Faden nicht beenden.
                logger.warning("Durchgang gescheitert", exc_info=True)
                gearbeitet = False
            if not gearbeitet:
                self._stopp.wait(TAKT_SEKUNDEN)

    def start(self) -> None:
        """Startet den Hintergrund-Faden."""
        if self._faden is not None:
            return
        self._faden = threading.Thread(
            target=self._schleife, name="pti-auftraege", daemon=True
        )
        self._faden.start()
        logger.info("Auftragsarbeiter gestartet")

    def stopp(self, frist: float = 5.0) -> None:
        """Beendet den Hintergrund-Faden mit Timeout."""
        self._stopp.set()
        if self._faden is not None:
            self._faden.join(timeout=frist)
            self._faden = None
        logger.info("Auftragsarbeiter beendet")
