"""Verarbeitungskette: Erkennung, Ersetzung und Rueckfuehrung.

Dies ist die Naht zwischen den drei unabhaengigen Bausteinen:

    Format-Adapter  →  Erkennungs-Engine  →  Vault
    (backend.adapters)  (backend.engine)     (backend.vault)

Der Adapter liefert Segmente reinen Textes, die Engine findet darin Entitaeten,
der Vault vergibt (und merkt sich) die Pseudonyme, und der Adapter schreibt die
Ersetzungen formaterhaltend zurueck.

Drei Regeln, die hier zentral durchgesetzt werden und nirgends sonst:

1. **Ersetzungen werden von rechts nach links angewandt.** Der Adapter erwartet
   Offsets, die sich auf den unveraenderten Segmenttext beziehen. Wuerde man von
   links beginnen, verschoebe jede Ersetzung alle folgenden Offsets.
2. **Ueberlappende Treffer sind bereits in der Engine aufgeloest**
   (``engine.merge``). Hier wird das nur noch geprueft, nicht repariert — ein
   Ueberlapp an dieser Stelle waere ein Fehler weiter oben und soll auffallen.
3. **Ein Fund gilt im ganzen Dokument** (:func:`propagate_findings`). Das
   NER-Modell entscheidet je Vorkommen: derselbe Name wird im einen Absatz
   erkannt und im naechsten uebersehen. Ersetzt wuerde dann nur die erkannte
   Stelle — im Ergebnisdokument stuende der Klarname weiter da, obwohl er in
   der Befundliste steht. Deshalb wird jeder Fundtext nach der Erkennung an
   allen weiteren Stellen nachgetragen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from presidio_analyzer import RecognizerResult
from sqlalchemy.orm import Session

from backend.adapters import get_adapter
from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.engine import analyzer as engine
from backend.engine import entities as ents
from backend.engine.merge import merge_overlaps
from backend.engine.recognizers.custom_terms import build_term_regex, coerce_term
from backend.vault import service as vault_service
from backend.vault.models import Vault

#: Protokolliert wird ausschliesslich Zaehlbares — nie Inhalte. Ein Klartext
#: im Log waere genau die Personendatenspur, die dieses Werkzeug beseitigt.
logger = logging.getLogger("pti.pipeline")

# Ein reiner Textabschnitt bekommt diese Segment-ID, damit Text- und
# Dokumentpfad dieselbe Datenstruktur benutzen koennen.
TEXT_SEGMENT_ID = "text"


@dataclass(frozen=True)
class Finding:
    """Ein einzelner Fund, angereichert um alles, was die Oberflaeche braucht."""

    segment_id: str
    entity_type: str
    start: int
    end: int
    score: float
    text: str
    #: ``True``, wenn dieser Fund nicht von der Engine kommt, sondern von
    #: :func:`propagate_findings` aus einem gleichlautenden Fund uebertragen
    #: wurde. Rein informativ — fuer die Ersetzung sind beide gleichwertig.
    propagated: bool = False

    @property
    def label(self) -> str:
        """Deutsches Anzeigelabel des Entitaetstyps."""
        return ents.get_label(self.entity_type)

    @property
    def confidence(self) -> str:
        """Ampelstufe fuer die Oberflaeche: gruen | gelb | rot.

        Die Schwellen entsprechen dem Konfidenz-Modell des Fachkonzepts
        (Abschnitt 13.3) und sind bewusst hier definiert und nicht im
        Frontend — die Bewertung eines Fundes ist eine fachliche Aussage,
        keine Darstellungsfrage.
        """
        if self.score >= 0.85:
            return "gruen"
        if self.score >= 0.5:
            return "gelb"
        return "rot"

    def as_dict(self) -> dict:
        """Serialisierung fuer die REST-Antwort."""
        return {
            "segment_id": self.segment_id,
            "entity_type": self.entity_type,
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "text": self.text,
            "propagated": self.propagated,
        }


@dataclass
class DetectionResult:
    """Ergebnis eines reinen Erkennungslaufs, ohne Ersetzung."""

    segments: list[Segment]
    findings: list[Finding] = field(default_factory=list)
    #: ``True``, wenn die KI-Schicht tatsaechlich gelaufen ist.
    #:
    #: ``False`` heisst: nicht angefordert, nicht konfiguriert — **oder** der
    #: Dienst hat bei einem der Schritte nicht geantwortet. Der Aufrufer muss
    #: wissen, was er in der Hand haelt; ein still schlechter geprueftes
    #: Ergebnis waere das gefaehrlichere Verhalten.
    #:
    #: „Nichts zu pruefen" gehoert seit Stufe D **nicht** mehr dazu: Die
    #: Zweitmeinung sieht das ganze Dokument, auch wenn die lokale Erkennung
    #: nichts gefunden hat.
    ki_geprueft: bool = False

    @property
    def char_count(self) -> int:
        """Gesamtzahl analysierter Zeichen — Grundlage der Nutzungserfassung."""
        return sum(len(s.text) for s in self.segments)

    def counts_by_type(self) -> dict[str, int]:
        """Anzahl Funde je Entitaetstyp. Enthaelt bewusst KEINE Inhalte."""
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.entity_type] = counts.get(f.entity_type, 0) + 1
        return counts


#: Rueckruf ``(erledigt, gesamt) -> None`` fuer den Fortschritt eines Laufs.
#:
#: Ausschliesslich Zaehlwerte, nie ein Text: Was hier durchgeht, landet ueber
#: ``job.fortschritt``/``job.gesamt`` in einer unverschluesselten Spalte und
#: geht ueber ``GET /jobs/{id}`` auch an die externe API.
Fortschrittsmelder = Callable[[int, int], None]


def detect_in_segments(
    segments: Sequence[Segment],
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
    propagate: bool = True,
    ki_pruefung: bool = False,
    ki=None,
    melde_fortschritt: Optional[Fortschrittsmelder] = None,
) -> DetectionResult:
    """Laesst die Engine ueber alle Segmente laufen.

    Jedes Segment wird einzeln analysiert: die Offsets muessen sich auf den
    jeweiligen Segmenttext beziehen, weil der Adapter genau damit
    zurueckschreibt. Ein Zusammenkleben aller Segmente waere schneller, wuerde
    aber Satzgrenzen ueber Absatz- und Zellengrenzen hinweg erfinden und die
    NER-Qualitaet verschlechtern.

    ``entities`` wird vorher auf das geschnitten, was diese Engine ueberhaupt
    bedienen kann (``engine.servable_entities``). Der Katalog in
    :mod:`backend.engine.entities` sagt naemlich nur, welche Typen wir
    *anbieten* — nicht, dass es fuer jeden auch einen registrierten Erkenner
    gibt. Die ``PTI_``-Typen entstehen ausschliesslich ueber die
    Begriffsliste: ohne einen Begriff dieses Typs existiert kein Erkenner
    dafuer, und Presidio quittiert eine Anfrage ohne jeden zustaendigen
    Erkenner mit einem ``ValueError`` — fuer die Aufruferin ein 500 auf eine
    voellig gueltige Kategorienwahl. Bleibt nach dem Schnitt nichts uebrig,
    gibt es hier schlicht keinen Befund; die Segmente (und damit ``char_count``
    fuer die Nutzungserfassung) bleiben erhalten.

    Dieser eine Ort deckt beide Wege ab — Text- wie Dokumentpfad laufen ueber
    diese Funktion.

    ``propagate`` steuert den Nachtrag gleichlautender Stellen
    (:func:`propagate_findings`). Nur zum Abschalten in Tests und in der
    Diagnose gedacht — im Betrieb immer an.

    ``ki_pruefung`` schaltet die KI-Schicht zu (Phase 3, Variante C). Vorgabe
    **aus**: Wer nicht ausdruecklich fragt, schickt keinen Text hinaus.
    ``ki`` ist der Einstiegspunkt fuer Tests; ohne ihn entsteht bei Bedarf ein
    :class:`~backend.ki.infomaniak.InfomaniakKi`.

    ``melde_fortschritt`` bekommt nach jedem Segment ``(erledigt, gesamt)``.
    **Gezaehlt wird der Segmentdurchlauf und nur er** — nicht der ganze Lauf.
    Das ist Absicht und keine Nachlaessigkeit: Der Durchlauf ist der Teil, der
    mit der Dokumentgroesse waechst (je Segment ein spaCy-Lauf), und er ist der
    einzige, der ueberhaupt eine natuerliche Einheit hat. Was danach kommt --
    KI-Schicht, Uebertragung, Zurueckschreiben -- laeuft nicht je Segment und
    liesse sich nur mit einem erfundenen Nenner in denselben Balken pressen.
    Der Zaehler steht deshalb eine Weile auf ``gesamt``, bevor der Auftrag auf
    ``fertig`` springt; die Oberflaeche sagt in dieser Zeit weiterhin „wird
    verarbeitet". Ein Balken, der bei 60 % stehen bleibt, weil jemand den
    Nenner geschaetzt hat, waere die schlechtere Luege.

    Der Rueckruf wird ungedrosselt gerufen -- einmal je Segment, bei einer
    grossen Tabelle also tausendfach. Wer ihn teuer beantwortet (der Arbeiter
    schreibt in die Datenbank), drosselt selbst; hier zu drosseln hiesse, jedem
    Aufrufer denselben Takt aufzuzwingen.
    """
    result = DetectionResult(segments=list(segments))

    if entities is not None:
        entities = engine.servable_entities(list(entities), custom_terms=custom_terms)
        if not entities:
            return result

    gesamt = len(segments)
    for nummer, segment in enumerate(segments, start=1):
        # Der Fortschritt wird auch fuer ein leeres Segment gemeldet, und zwar
        # VOR dem `continue`: Sonst bliebe der Zaehler in einem Dokument mit
        # vielen Leerabsaetzen stehen, obwohl der Lauf vorankommt -- und im
        # Extremfall (alle restlichen Segmente leer) erreichte er nie `gesamt`.
        if melde_fortschritt is not None:
            melde_fortschritt(nummer, gesamt)
        if not segment.text.strip():
            continue
        for r in engine.analyze(
            text=segment.text,
            entities=entities,
            custom_terms=custom_terms,
        ):
            result.findings.append(
                Finding(
                    segment_id=segment.id,
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    score=float(r.score),
                    text=segment.text[r.start : r.end],
                )
            )

    # ── KI-Schicht (Phase 3, Variante C) ──────────────────────────────────
    # Sitzt bewusst VOR propagate_findings: Korrigiert die KI `Sandra` zu
    # `Sandra Zbinden`, traegt die Uebertragung anschliessend den
    # VOLLSTAENDIGEN Namen durchs Dokument — auch an Stellen, die spaCy
    # uebersehen hat. In umgekehrter Reihenfolge bliebe die Korrektur auf die
    # eine Fundstelle beschraenkt, und die halbe Fassung stuende weiter da.
    if ki_pruefung:
        # Die aeussere Sicherung. `_ki_pruefen` faengt bereits jeden `KiFehler`
        # — aber am 2026-08-25 riss ein `AttributeError` aus der Auswertung
        # einer missgestalteten Antwort den ganzen Lauf. Die Zusage dieser
        # Schicht lautet „ein Ausfall kostet nie eine Erkennung", und die darf
        # nicht davon abhaengen, dass jede kuenftige Aenderung an jede
        # Ausnahmeart gedacht hat.
        try:
            result.findings, result.ki_geprueft = _ki_pruefen(
                result.segments, result.findings, ki
            )
        except Exception:
            logger.warning("KI-Pruefung unerwartet abgebrochen.", exc_info=True)
            result.ki_geprueft = False
        # Reine Messung, danach und getrennt: Sie nimmt die Fundliste nur
        # lesend und gibt nichts zurueck. `ki_geprueft` bleibt davon
        # unberuehrt — das Flag sagt aus, ob die ANGEWANDTE Namenspruefung
        # vollstaendig durchlief, und das tut sie unabhaengig davon.
        _ki_messen_ort_organisation(result.segments, result.findings, ki)

    if propagate:
        result.findings = propagate_findings(result.segments, result.findings)

    # ── Stufe D: Zweitmeinung auf dem maskierten Volltext ─────────────────
    # NACH der Uebertragung, damit die Maske moeglichst viel abdeckt: Jeder
    # Fund, den `propagate_findings` nachtraegt, ist einer weniger, der im
    # hinausgehenden Text im Klartext stuende.
    #
    # Danach wird ein zweites Mal uebertragen. Ohne das haette Stufe D genau
    # das Fehlerbild, gegen das PR #8 gebaut wurde: Ein neu gefundener Name
    # gaelte nur an der einen gemeldeten Stelle, und im Ergebnisdokument
    # stuende er anderswo weiter im Klartext.
    if ki_pruefung:
        try:
            result.findings, vollstaendig = _ki_zweitmeinung(
                result.segments, result.findings, ki
            )
        except Exception:
            logger.warning("Zweitmeinung unerwartet abgebrochen.", exc_info=True)
            vollstaendig = False
        if not vollstaendig:
            result.ki_geprueft = False
        if propagate:
            result.findings = propagate_findings(result.segments, result.findings)

    return result


#: Wie oft ein Stueck hoechstens angefragt wird — Erstversuch eingerechnet.
#:
#: Zwei, nicht drei — und im Normalbetrieb greift der zweite nie.
#:
#: Mit Apertus gelangen am 2026-08-24 alle neun Anfragen des Golden-Sets beim
#: ersten Versuch. Die Wiederholung ist damit **Versicherung, kein
#: Werkzeug**: Sie kostet nichts, solange nichts schiefgeht, und faengt
#: einen einzelnen Aussetzer ab.
#:
#: Wichtig ist, was sie NICHT kann. Ein Modell, das bei einer bestimmten
#: Eingabe reproduzierbar degeneriert, tut das beim zweiten Versuch genauso
#: — gemessen: drei von drei Wiederholungen scheiterten damals identisch.
#: Dagegen hilft nur ein anderes Modell, nie ein weiterer Versuch.
MAX_KI_VERSUCHE = 2


def _frage_mit_wiederholung(ki, stueck, gesamt_fenster: int):
    """Fragt ein Stueck an und wiederholt hoechstens einmal.

    Wiederholt wird nur, was Aussicht auf Erfolg hat: Zeitgrenzen,
    Netzwerkfehler, 429 und 5xx. Ein HTTP 400 oder ein fehlender Schluessel
    aendern sich beim zweiten Mal nicht (``KiFehler.wiederholbar``).

    Gibt die Antwort zurueck oder ``None``, wenn alle Versuche scheiterten.
    """
    from backend.ki import namen
    from backend.ki.infomaniak import KiFehler

    for versuch in range(1, MAX_KI_VERSUCHE + 1):
        try:
            return ki.chat_json(namen.baue_nachrichten(stueck), namen.SCHEMA)
        except KiFehler as fehler:
            letzter = versuch >= MAX_KI_VERSUCHE or not getattr(
                fehler, "wiederholbar", True
            )
            logger.warning(
                "KI-Pruefung fehlgeschlagen (Versuch %d, %d von %d Fenstern "
                "betroffen)%s: %s",
                versuch,
                len(stueck),
                gesamt_fenster,
                "" if letzter else " — wird wiederholt",
                fehler,
            )
            if letzter:
                return None
    return None


def _ki_pruefen(segments, findings, ki) -> tuple[list[Finding], bool]:
    """Laesst die KI die Namensgrenzen pruefen. Faellt nie hart aus.

    Jeder Fehler — nicht konfiguriert, Zeitgrenze, Fehlerstatus, leere oder
    ungueltige Antwort — fuehrt zum lokalen Ergebnis und ``False``. Der
    Aufrufer zeigt das an; still schlechter zu erkennen waere die
    gefaehrlichere Variante.

    Der Import steht bewusst **in** der Funktion: Ohne angeforderte
    KI-Pruefung wird ``backend.ki`` nie geladen, und der bisherige Weg bleibt
    frei von jeder zusaetzlichen Abhaengigkeit.
    """
    from backend.config import get_settings
    from backend.ki import namen
    from backend.ki.infomaniak import InfomaniakKi, ist_verfuegbar

    einstellungen = get_settings()
    if ki is None:
        if not ist_verfuegbar(einstellungen):
            logger.info("KI-Pruefung angefordert, aber nicht konfiguriert.")
            return list(findings), False
        ki = InfomaniakKi(einstellungen)

    fenster = namen.baue_fenster(segments, findings)
    if not fenster:
        # Nichts zu pruefen ist kein Ausfall, sondern ein vollstaendiger Lauf.
        #
        # Bis Stufe D stand hier `False`, und das war damals vertretbar: Ohne
        # Fundkandidaten lief die KI-Schicht ueberhaupt nicht. Seit die
        # Zweitmeinung auch dann fragt — gerade dann —, hiesse `False` dem
        # Nutzer gegenueber „die KI konnte nicht pruefen", waehrend sie das
        # ganze Dokument gesehen hat. Ein Hinweis, der ohne Grund erscheint,
        # bringt Menschen dazu, Hinweise zu uebergehen.
        return list(findings), True

    # Gestueckelt, weil die Antwortzeit mit der Fensterzahl waechst (gemessen:
    # rund 5 s Grundlast plus 1.3 s je Fenster). Faellt ein Stueck aus, bleiben
    # nur dessen Fenster unkorrigiert statt des ganzen Dokuments.
    aktuell = list(findings)
    stuecke = namen.stueckle(fenster)
    gelungen = 0
    verworfen_gesamt = 0

    for stueck in stuecke:
        antwort = _frage_mit_wiederholung(ki, stueck, len(fenster))
        if antwort is None:
            continue
        aktuell, verworfen = namen.wende_an(aktuell, stueck, antwort)
        verworfen_gesamt += verworfen
        gelungen += 1

    logger.info(
        "KI-Pruefung: %d/%d Anfragen gelungen, %d Fenster, %d Vorschlaege "
        "verworfen, Modell %s",
        gelungen,
        len(stuecke),
        len(fenster),
        verworfen_gesamt,
        # Der ANGEFRAGTE Name, nicht der aus der Antwort: Manche Modelle
        # nennen sich dort anders (`large-endpoint`).
        einstellungen.infomaniak_ai_modell,
    )
    # `ki_geprueft` nur, wenn ALLE Stuecke durchkamen. Ein teilweise geprueftes
    # Ergebnis als geprueft auszuweisen waere die gefaehrlichere Aussage: Der
    # Mensch verliesse sich dann auf eine Pruefung, die einen Teil des
    # Dokuments nie gesehen hat.
    return aktuell, gelungen == len(stuecke)


def _ki_zweitmeinung(segments, findings, ki) -> tuple[list[Finding], bool]:
    """Fragt zum **maskierten** Volltext, ob noch Personendaten darin stehen.

    Stufe D — die Umkehrung der Fensterloesung. Statt mehr Rohtext
    hinauszugeben, geht der Text ohne die erkannten Namen hinaus; was gemeldet
    wird, hat die lokale Erkennung uebersehen. Die Fachlogik samt Schranken
    steht in :mod:`backend.ki.zweitmeinung`.

    Ein ausgefallenes Stueck kostet die Erkennung nichts, wird aber ueber den
    Rueckgabewert angezeigt: Der Aufrufer setzt dann ``ki_geprueft`` auf
    ``False``, und die Oberflaeche weist darauf hin. Anders als bei der
    Messung zu Ort und Organisation ist das hier richtig — Stufe D **wird
    angewandt**, ihr Ausfall aendert also das Ergebnis.

    Ist der Betriebsschalter ``KI_ZWEITMEINUNG`` aus, gilt der Lauf als
    vollstaendig: Das ist eine bewusste Betriebsentscheidung und kein Ausfall.
    """
    from backend.config import get_settings
    from backend.ki import zweitmeinung
    from backend.ki.infomaniak import InfomaniakKi, ist_verfuegbar

    einstellungen = get_settings()
    if not einstellungen.ki_zweitmeinung:
        return list(findings), True

    if ki is None:
        if not ist_verfuegbar(einstellungen):
            return list(findings), False
        ki = InfomaniakKi(einstellungen)

    nach_segment: dict[str, list[Finding]] = {}
    for fund in findings:
        nach_segment.setdefault(fund.segment_id, []).append(fund)

    stuecke = zweitmeinung.stueckle(segments, nach_segment)
    if not stuecke:
        return list(findings), True

    aktuell = list(findings)
    gelungen = 0
    for stueck in stuecke:
        try:
            antwort = ki.chat_json(
                zweitmeinung.baue_nachrichten(stueck), zweitmeinung.SCHEMA
            )
        except Exception:
            logger.warning("Zweitmeinung: ein Stueck ausgefallen.", exc_info=True)
            continue
        aktuell = zweitmeinung.wende_an(stueck, aktuell, antwort)
        gelungen += 1

    logger.info(
        "Zweitmeinung: %d/%d Stuecke gelungen, %d Funde danach",
        gelungen,
        len(stuecke),
        len(aktuell),
    )
    return aktuell, gelungen == len(stuecke)


#: Typen, zu denen die KI **befragt, aber nicht gehoert** wird.
#:
#: Fuenf der sechs verbleibenden Fehlalarme des Golden-Sets sind
#: Gattungswoerter, die als Ort oder Organisation durchgehen (``Fallnummer``,
#: ``Kontrollschild``, ``Intern``). Die Wortart-Regel kann sie nachweislich
#: nicht trennen — siehe ``backend/engine/wortarten.py``.
#:
#: Die KI koennte es; sie hat ``Police cantonale`` unaufgefordert als
#: Nicht-Person bezeichnet. Wieviel sie in **echten** Dokumenten taugt, weiss
#: aber niemand, und das Golden-Set hat dafuer zu wenige Faelle. Deshalb wird
#: hier nur gefragt und protokolliert. Ob daraus je ein Eingriff wird,
#: entscheidet der Auftraggeber an gemessenen Zahlen — nicht an einer
#: Vermutung.
MESSTYPEN = ("LOCATION", "ORGANIZATION")


def _ki_messen_ort_organisation(segments, findings, ki) -> None:
    """Fragt die KI zu Ort- und Organisationsfunden und aendert nichts.

    Gibt bewusst ``None`` zurueck und nimmt ``findings`` nur lesend: Es gibt
    kein Gegenstueck zu :func:`backend.ki.namen.wende_an`, damit aus der
    Messung nicht versehentlich ein Eingriff wird. Der Waechter darueber ist
    ``test_messung_zu_ort_und_organisation_aendert_keinen_fund``.

    Faellt irgendetwas aus, kostet das nichts ausser der Messung selbst —
    deshalb faengt der Block **jede** Ausnahme. Das ist hier richtig und nicht
    nachlaessig: Eine Erkennung, die an einem Nebenprodukt scheitert, waere
    ein Rueckschritt gegenueber dem Zustand ohne Messung.
    """
    try:
        from backend.config import get_settings
        from backend.ki import namen
        from backend.ki.infomaniak import InfomaniakKi, ist_verfuegbar

        if ki is None:
            einstellungen = get_settings()
            if not ist_verfuegbar(einstellungen):
                return
            ki = InfomaniakKi(einstellungen)

        fenster = namen.baue_fenster(segments, findings, typen=MESSTYPEN)
        if not fenster:
            return

        for stueck in namen.stueckle(fenster):
            antwort = ki.chat_json(
                namen.baue_eigenname_nachrichten(stueck), namen.SCHEMA_EIGENNAME
            )
            nach_nummer = {f.nummer: f for f in stueck}
            for eintrag in antwort.get("funde", []):
                gefragt = nach_nummer.get(eintrag.get("nummer"))
                if gefragt is None:
                    continue
                # Festes Praefix zum Filtern im Protokoll; kein Dokumentbezug
                # und kein Segmenttext — nur der Fundtext, der ohnehin schon
                # in der Fundliste vor dem Nutzer steht.
                logger.info(
                    "KI-Messung typ=%s ist_eigenname=%s text=%r",
                    findings[gefragt.finding_index].entity_type,
                    eintrag.get("ist_eigenname"),
                    gefragt.markiert,
                )
    except Exception:
        logger.warning("KI-Messung zu Ort/Organisation ausgefallen.", exc_info=True)


# ── Uebertragung eines Fundes auf gleichlautende Stellen ────────────────────
#
# Warum das noetig ist, steht im Modulkopf unter Regel 3. Hier die Grenzen, die
# dabei gezogen werden — jede einzelne verhindert einen konkreten Schaden.

#: Mindestlaenge eines Fundtexts, damit er uebertragen wird.
#:
#: Kuerzeres traegt zu wenig Eigenheit: ein zweibuchstabiges Kuerzel steckt in
#: zu vielen harmlosen Stellen, und ein Fehlalarm darauf wuerde sich ueber das
#: ganze Dokument vervielfachen — aus einem falsch ersetzten Wort wuerden
#: dreissig.
MIN_UEBERTRAGUNGSLAENGE = 3


@dataclass(frozen=True)
class _Uebertragung:
    """Ein Fundtext, der im ganzen Dokument nachgetragen wird."""

    begriff: str
    entity_type: str
    score: float
    muster: re.Pattern


def _wortzeichen(zeichen: str) -> bool:
    """Prueft, ob das Zeichen fuer die Wortgrenze als Wortbestandteil zaehlt."""
    return zeichen.isalnum() or zeichen == "_"


def wortgrenzen_muster(begriff: str) -> re.Pattern:
    r"""Sucht ``begriff`` wortgrenzengenau — aber nur, wo eine Grenze Sinn ergibt.

    ``\bMeier\b`` waere falsch, sobald ein Begriff mit einem Satzzeichen
    beginnt oder endet: ``\b`` verlangt dort einen Wortwechsel, den es nicht
    gibt, und der Begriff wuerde nie gefunden. Die Grenze wird deshalb nur an
    der Seite gesetzt, an der der Begriff selbst mit einem Wortzeichen anfaengt
    bzw. aufhoert.

    Bewusst **gross-/kleinschreibungsempfindlich**: die uebertragenen Typen
    sind Eigennamen und stehen gross. Ein tolerantes ``Bund`` wuerde jedes
    ``bund`` in ``Verbund`` mitnehmen — die Wortgrenze schuetzt davor, die
    Kleinschreibung ist die zweite Sicherung.
    """
    vorne = r"(?<!\w)" if _wortzeichen(begriff[0]) else ""
    hinten = r"(?!\w)" if _wortzeichen(begriff[-1]) else ""
    return re.compile(vorne + re.escape(begriff) + hinten)


def _uebertragbare_begriffe(findings: Sequence[Finding]) -> list[_Uebertragung]:
    """Sammelt die Fundtexte, die uebertragen werden — laengste zuerst.

    Uebertragen wird nur, was aus dem NER-Modell stammt
    (:data:`~backend.engine.entities.NER_ENTITIES`). Muster- und
    Begriffslisten-Erkenner finden ihren Text ohnehin an jeder Stelle; ihre
    Treffer nachzutragen brauchte niemand und koennte nur einen Ueberlapp
    wieder einfuehren, den ``engine.merge`` gerade aufgeloest hat.

    Die Sortierung nach absteigender Laenge ist wesentlich: stehen
    ``Andreas Meier`` und ``Meier`` beide in der Liste, muss der laengere
    Begriff seine Stellen zuerst belegen. Sonst schnappt sich ``Meier`` das
    halbe Vorkommen und der Rest bliebe als ``Andreas `` stehen.
    """
    je_text: dict[str, list[Finding]] = {}
    for f in findings:
        if f.entity_type not in ents.NER_ENTITIES:
            continue
        if f.text != f.text.strip():
            # Ein Span mit Leerraum am Rand ist ein unsauberer Treffer; ihn zu
            # vervielfaeltigen zoege den Schnittfehler durchs ganze Dokument.
            continue
        if len(f.text) < MIN_UEBERTRAGUNGSLAENGE:
            continue
        if not any(zeichen.isalpha() for zeichen in f.text):
            continue
        je_text.setdefault(f.text, []).append(f)

    uebertragungen = []
    for text, treffer in je_text.items():
        typ, score = _mehrheitstyp(treffer)
        uebertragungen.append(
            _Uebertragung(
                begriff=text,
                entity_type=typ,
                score=score,
                muster=wortgrenzen_muster(text),
            )
        )
    uebertragungen.sort(key=lambda u: (-len(u.begriff), u.begriff))
    return uebertragungen


def _mehrheitstyp(treffer: Sequence[Finding]) -> tuple[str, float]:
    """Bestimmt den Typ, wenn die Engine denselben Text uneinheitlich einordnet.

    Kommt regelmaessig vor: ``Zuerich`` ist im einen Absatz ``LOCATION``, im
    anderen Teil einer ``ORGANIZATION``. Entschieden wird nach dem hoechsten
    Score, bei Gleichstand nach der Haeufigkeit, zuletzt alphabetisch. Fachlich
    ist die letzte Stufe beliebig — sie ist nur dafuer da, dass zwei Laeufe ueber
    dasselbe Dokument garantiert dasselbe Ergebnis liefern (sonst wackelten die
    Pseudonyme im Vault).
    """
    je_typ: dict[str, list[float]] = {}
    for f in treffer:
        je_typ.setdefault(f.entity_type, []).append(f.score)
    typ = min(je_typ, key=lambda t: (-max(je_typ[t]), -len(je_typ[t]), t))
    return typ, max(je_typ[typ])


def propagate_findings(
    segments: Sequence[Segment], findings: Sequence[Finding]
) -> list[Finding]:
    """Traegt jeden Fundtext an allen weiteren Stellen des Dokuments nach.

    Der Kern der Sache: das NER-Modell entscheidet **je Vorkommen**. Steht ein
    Name in Absatz 3 in einem Satz, den das Modell versteht, und in Absatz 9 in
    einer Stichwortzeile ohne Kontext, wird er einmal gemeldet und einmal nicht.
    Ersetzt wuerde ohne diesen Schritt nur die gemeldete Stelle — der Klarname
    bliebe im Dokument stehen, obwohl er in der Befundliste steht. Genau dieser
    Widerspruch war der Anlass.

    Die nachgetragenen Funde sind von den echten nicht zu unterscheiden: sie
    tragen denselben Typ und denselben Score und laufen durch dieselbe
    Ersetzung. Nur ``Finding.propagated`` verraet ihre Herkunft, damit die
    Oberflaeche sie kennzeichnen kann.

    :param segments: Alle Segmente des Dokuments — auch die ohne Fund, denn
        genau dort sitzen die uebersehenen Stellen.
    :param findings: Die Funde der Engine.
    :return: Die urspruenglichen Funde plus die nachgetragenen, in
        Dokumentreihenfolge und garantiert ueberlappungsfrei.
    """
    uebertragungen = _uebertragbare_begriffe(findings)
    if not uebertragungen:
        return list(findings)

    je_segment: dict[str, list[Finding]] = {}
    for f in findings:
        je_segment.setdefault(f.segment_id, []).append(f)

    ergaenzt: list[Finding] = []
    for segment in segments:
        vorhanden = je_segment.pop(segment.id, [])
        kandidaten = list(vorhanden)
        # Deckungsgleiche Stellen sind tabu — und zwar unabhaengig vom Typ.
        #
        # Dass ein Nachtrag den Fund dupliziert, aus dem er stammt, ist der
        # harmlose Teil. Der andere waere ein Schaden: ordnet die Engine
        # denselben Text mehrheitlich anders ein (``Sandra`` haeufiger als Ort
        # denn als Person), wuerde der Nachtrag einen korrekt erkannten Fund auf
        # seiner eigenen Stelle umetikettieren. Uebertragen wird deshalb nur,
        # was eine Stelle *hinzufuegt* oder *erweitert* — nie, was eine
        # bestehende bloss umbenennt.
        bekannt = {(f.start, f.end) for f in vorhanden}

        for u in uebertragungen:
            for treffer in u.muster.finditer(segment.text):
                schluessel = (treffer.start(), treffer.end())
                if schluessel in bekannt:
                    continue
                bekannt.add(schluessel)
                kandidaten.append(
                    Finding(
                        segment_id=segment.id,
                        entity_type=u.entity_type,
                        start=treffer.start(),
                        end=treffer.end(),
                        score=u.score,
                        text=u.begriff,
                        propagated=True,
                    )
                )

        ergaenzt.extend(_ohne_ueberlapp(kandidaten))

    # Funde zu Segmenten, die nicht mitgeliefert wurden, bleiben unangetastet
    # stehen — sie zu verlieren waere schlimmer als eine unsortierte Liste.
    for rest in je_segment.values():
        ergaenzt.extend(rest)
    return ergaenzt


def _ohne_ueberlapp(kandidaten: Sequence[Finding]) -> list[Finding]:
    """Loest Ueberlappungen innerhalb eines Segments auf — mit der Engine-Regel.

    Bewusst ueber :func:`backend.engine.merge.merge_overlaps` und nicht ueber
    eine eigene Pruefung: es soll im ganzen Projekt genau **eine** Regel geben,
    welcher von zwei konkurrierenden Treffern gewinnt. Das ist hier nicht
    Kosmetik, sondern der Kern des Falls:

    Das Modell erkennt in einer Stichwortzeile oft nur den Vornamen
    (``Sandra``), waehrend es im Fliesstext den ganzen Namen sieht
    (``Sandra Zbinden``). Wuerde der bereits erkannte Vorname seine Stelle
    einfach behalten, bliebe der Nachname im Dokument stehen — und der
    Nachtrag haette den Fall nur halb geloest. Regel 2 des Merges (bei
    gleichem Score gewinnt der laengere Span) raeumt genau das auf.
    """
    if len(kandidaten) < 2:
        return list(kandidaten)

    je_stelle: dict[tuple[int, int, str], Finding] = {}
    roh = []
    for f in kandidaten:
        je_stelle.setdefault((f.start, f.end, f.entity_type), f)
        roh.append(
            RecognizerResult(
                entity_type=f.entity_type, start=f.start, end=f.end, score=f.score
            )
        )
    return [je_stelle[(r.start, r.end, r.entity_type)] for r in merge_overlaps(roh)]


def _select(
    findings: Sequence[Finding],
    selected: Optional[Sequence[dict]],
    *,
    entities: Optional[Iterable[str]] = None,
) -> list[Finding]:
    """Filtert die Funde auf die vom Menschen bestaetigte Auswahl.

    Ohne Auswahl gilt die Default-Policy je Entitaetstyp: mehrdeutige Typen wie
    Postleitzahl und Kontrollschild stehen bewusst auf ``keep`` und werden nicht
    ungefragt ersetzt (sonst zerstoert ein Fehlalarm den Text).
    """
    if selected is not None:
        wanted = {
            (s.get("segment_id"), s.get("start"), s.get("end")) for s in selected
        }
        return [f for f in findings if (f.segment_id, f.start, f.end) in wanted]

    if entities is not None:
        # Ausdrueckliche Wahl schlaegt die Default-Policy: wer die PLZ anhakt,
        # will sie ersetzt haben — nicht nur markiert.
        erlaubt = set(entities)
        return [f for f in findings if f.entity_type in erlaubt]

    return [
        f for f in findings if ents.get_policy(f.entity_type) is not ents.Policy.KEEP
    ]


def feste_pseudonyme(
    custom_terms: Optional[Sequence],
) -> list[tuple[re.Pattern, str, str]]:
    """Baut die Nachschlagetabelle fuer feste Pseudonyme aus der Begriffsliste.

    Ein festes Pseudonym haengt am **Begriff**, nicht an der Fundstelle. Es
    hier ueber dasselbe Regex aufzuloesen, mit dem der Erkenner gesucht hat,
    statt es durch Presidio, ``merge_overlaps`` und die Fund-Uebertragung
    durchzureichen, hat einen praktischen Grund: So bekommt auch ein
    *nachgetragener* Fund (:func:`propagate_findings`) sein festes Pseudonym,
    ohne dass die Uebertragung etwas davon wissen muss.

    :return: Je Begriff mit festem Pseudonym ein Tripel
        ``(Muster, entity_type, Pseudonym)``, nach Begriff sortiert — damit
        bei zwei passenden Begriffen immer derselbe gewinnt.
    """
    tabelle: list[tuple[re.Pattern, str, str]] = []
    for item in custom_terms or []:
        term = coerce_term(item)
        if not term.replacement:
            continue
        try:
            muster = re.compile(build_term_regex(term))
        except re.error:  # pragma: no cover - nur bei kaputtem Begriff
            continue
        tabelle.append((muster, term.entity_type, term.replacement))
    tabelle.sort(key=lambda eintrag: (eintrag[2], eintrag[1]))
    return tabelle


def _festes_pseudonym(
    finding: Finding, tabelle: Sequence[tuple[re.Pattern, str, str]]
) -> Optional[str]:
    """Sucht das feste Pseudonym zu einem Fund, falls es eines gibt.

    ``fullmatch``, nicht ``search``: Der Fundtext muss dem Begriff **ganz**
    entsprechen. Sonst bekaeme ein laengerer Treffer, der einen Listenbegriff
    nur enthaelt (``Talblick AG`` gegenueber dem Begriff ``Talblick``), dessen
    festes Pseudonym — und im Dokument stuende danach ein Symbol, das fuer
    einen anderen Klartext steht.
    """
    for muster, entity_type, pseudonym in tabelle:
        if finding.entity_type == entity_type and muster.fullmatch(finding.text):
            return pseudonym
    return None


def build_replacements(
    session: Session,
    vault: Optional[Vault],
    findings: Sequence[Finding],
    custom_terms: Optional[Sequence] = None,
) -> tuple[dict[str, list[Replacement]], dict[str, str]]:
    """Vergibt Pseudonyme und baut die Ersetzungsliste je Segment.

    Mit ``vault`` ist der Vorgang umkehrbar: der Vault merkt sich die Zuordnung
    verschluesselt, und derselbe Klartext bekommt stabil dasselbe Pseudonym —
    auch ueber Segmentgrenzen und ueber mehrere Dokumente hinweg.

    Ohne ``vault`` (anonymer Modus) wird nur der Typ eingesetzt und nichts
    gespeichert. Das ist endgueltig und nicht rueckfuehrbar.

    ``custom_terms`` liefert die festen Pseudonyme der Begriffsliste. Auch sie
    landen im Vault — sonst fuende die Rueckfuehrung ihr Symbol nicht wieder.
    """
    per_segment: dict[str, list[Replacement]] = {}
    mapping: dict[str, str] = {}
    tabelle = feste_pseudonyme(custom_terms)

    for f in findings:
        fest = _festes_pseudonym(f, tabelle) if tabelle else None
        if vault is not None:
            pseudonym = vault_service.get_or_create_pseudonym(
                session, vault, f.entity_type, f.text, festes_pseudonym=fest
            )
        else:
            pseudonym = fest or f"[[{f.entity_type}]]"
        mapping[f.text] = pseudonym
        per_segment.setdefault(f.segment_id, []).append(
            Replacement(start=f.start, end=f.end, text=pseudonym)
        )

    # Rechts nach links, damit frueher angewandte Ersetzungen die Offsets der
    # spaeteren nicht verschieben. Der Adapter verlaesst sich darauf.
    for replacements in per_segment.values():
        replacements.sort(key=lambda r: r.start, reverse=True)
        _assert_no_overlap(replacements)

    return per_segment, mapping


def _assert_no_overlap(replacements: Sequence[Replacement]) -> None:
    """Bricht ab, wenn sich zwei Ersetzungen ueberlappen.

    Das darf nicht vorkommen: ``engine.merge`` loest Ueberlappungen bereits auf.
    Traete es hier auf, wuerde stillschweigend Text zerstoert — lieber laut
    scheitern als ein korruptes Dokument ausliefern.
    """
    for later, earlier in zip(replacements, replacements[1:]):
        if earlier.end > later.start:
            raise ValueError(
                "Ueberlappende Ersetzungen "
                f"[{earlier.start},{earlier.end}) und [{later.start},{later.end}) — "
                "das haette die Zusammenfuehrung in engine.merge aufloesen muessen."
            )


def anonymize_text(
    session: Session,
    vault: Optional[Vault],
    text: str,
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
    selected: Optional[Sequence[dict]] = None,
    ki_pruefung: bool = False,
) -> tuple[str, DetectionResult, list[Finding]]:
    """Pseudonymisiert einen reinen Text. Gibt Ergebnis, Befund und Auswahl zurueck."""
    segment = Segment(id=TEXT_SEGMENT_ID, text=text, location=None)
    detection = detect_in_segments(
        [segment],
        entities=entities,
        custom_terms=custom_terms,
        ki_pruefung=ki_pruefung,
    )
    applied = _select(detection.findings, selected, entities=entities)
    per_segment, _ = build_replacements(session, vault, applied, custom_terms)

    out = text
    for r in per_segment.get(TEXT_SEGMENT_ID, []):
        out = out[: r.start] + r.text + out[r.end :]
    return out, detection, applied


def anonymize_document(
    session: Session,
    vault: Optional[Vault],
    data: bytes,
    filename: str,
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
    selected: Optional[Sequence[dict]] = None,
    strip_metadata: bool = True,
    ki_pruefung: bool = False,
    melde_fortschritt: Optional[Fortschrittsmelder] = None,
) -> tuple[bytes, DetectionResult, list[Finding]]:
    """Pseudonymisiert ein Dokument formaterhaltend.

    ``strip_metadata`` ist standardmaessig an: Autor und Firma in den
    Dokumenteigenschaften sind Personendaten wie jede andere auch, stehen aber
    ausserhalb des Fliesstextes und wuerden von der Erkennung nie erfasst.

    ``melde_fortschritt`` reicht bis in den Segmentdurchlauf durch (siehe
    :func:`detect_in_segments`) — im synchronen Pfad ungenutzt, im
    Auftragspfad die Quelle von ``job.fortschritt``.
    """
    adapter: DocumentAdapter = get_adapter(filename)
    segments = adapter.extract_segments(data)
    detection = detect_in_segments(
        segments,
        entities=entities,
        custom_terms=custom_terms,
        ki_pruefung=ki_pruefung,
        melde_fortschritt=melde_fortschritt,
    )
    applied = _select(detection.findings, selected, entities=entities)
    per_segment, _ = build_replacements(session, vault, applied, custom_terms)

    out = adapter.apply_replacements(data, per_segment)
    if strip_metadata:
        out = adapter.strip_metadata(out)
    return out, detection, applied


def reverse_text(session: Session, vault: Vault, text: str) -> str:
    """Fuehrt einen pseudonymisierten Text mithilfe des Vaults zurueck."""
    return vault_service.reverse(session, vault, text)


def reverse_document(
    session: Session,
    vault: Vault,
    data: bytes,
    filename: str,
    *,
    melde_fortschritt: Optional[Fortschrittsmelder] = None,
) -> bytes:
    """Fuehrt ein pseudonymisiertes Dokument formaterhaltend zurueck.

    Gegenstueck zu :func:`anonymize_document`: statt Entitaeten zu suchen,
    werden die Pseudonyme im Text durch die entschluesselten Originale ersetzt.
    Die Ersetzung selbst macht der Vault-Dienst in einem einzigen Durchlauf.

    ``melde_fortschritt`` zaehlt hier dieselbe Einheit wie beim
    Pseudonymisieren — das Segment —, damit die Oberflaeche fuer beide
    Auftragsarten denselben Satz anzeigen kann. Der Durchlauf ist hier
    deutlich billiger (kein NER, nur ein Nachschlagen je Segment); die
    Meldung kostet trotzdem nichts, weil der Aufrufer drosselt.
    """
    adapter: DocumentAdapter = get_adapter(filename)
    segments = adapter.extract_segments(data)

    per_segment: dict[str, list[Replacement]] = {}
    gesamt = len(segments)
    for nummer, segment in enumerate(segments, start=1):
        if melde_fortschritt is not None:
            melde_fortschritt(nummer, gesamt)
        restored = vault_service.reverse(session, vault, segment.text)
        if restored != segment.text:
            # Das ganze Segment auf einmal ersetzen. Feiner geht es nicht, weil
            # der Vault-Dienst bewusst in einem Durchlauf arbeitet und keine
            # Einzeloffsets herausgibt (das verhindert Doppelersetzungen, wenn
            # ein Klartext selbst wie ein Pseudonym aussieht).
            per_segment[segment.id] = [
                Replacement(start=0, end=len(segment.text), text=restored)
            ]

    return adapter.apply_replacements(data, per_segment)
