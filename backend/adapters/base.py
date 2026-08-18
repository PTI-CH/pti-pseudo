"""Schnittstelle aller Dokument-Adapter des PTI-Pseudonymisierers.

Ein *Adapter* ist die Brücke zwischen einem Dateiformat und der formatlosen
Engine. Die Engine (``backend/engine/``) versteht ausschliesslich ``str`` — sie
weiss nichts von Word-Runs, Zeichensätzen oder ZIP-Containern. Der Adapter
übernimmt beide Übersetzungsrichtungen:

1. **Hinweg** — ``extract_segments()`` zerlegt das Dokument in eine Liste von
   :class:`Segment`. Jedes Segment trägt reinen Text, auf dem die Erkennung
   läuft, und ein **opakes** ``location``-Feld, das der Adapter (und nur er)
   interpretieren kann.
2. **Rückweg** — ``apply_replacements()`` bekommt dieselben Bytes und pro
   Segment-ID eine Liste von :class:`Replacement`. Die Offsets darin beziehen
   sich auf ``Segment.text``, also auf den Text, den der Adapter selbst
   geliefert hat. Der Adapter schreibt die Ersetzungen **formaterhaltend**
   zurück und gibt neue Bytes aus.

Der zentrale Vertrag lautet: **Der Adapter ist zustandslos.** Beide Methoden
bekommen die Original-Bytes übergeben. Es gibt kein „geöffnetes Dokument", das
zwischen den Aufrufen lebt. Das ist bewusst so:

* Die API kann Analyse und Anwendung auf zwei getrennte HTTP-Requests verteilen
  (``/analyze`` und ``/anonymize``), ohne Server-Zustand vorzuhalten.
* Ein ``location`` darf deshalb **keine Objektreferenz** sein (kein
  lxml-Element, kein ``python-docx``-Paragraph). Es muss eine *Adresse* sein,
  die beim erneuten Öffnen derselben Bytes wieder dasselbe Element trifft —
  etwa ``(Part-Name, Absatz-Index)`` beim DOCX-Adapter oder ``(Zeilennummer)``
  beim CSV-Adapter.

Daraus folgt die **Determinismus-Pflicht**: Zweimaliges ``extract_segments()``
auf identische Bytes muss identische IDs, Texte und Locations liefern. Sonst
zeigen die Offsets aus dem Analyse-Request beim Anwendungs-Request ins Leere.

Weitere Zusicherungen, auf die sich Aufrufer verlassen dürfen:

* ``Segment.id`` ist innerhalb eines Dokuments eindeutig.
* Die Segmente **überlappen sich nicht**: kein Zeichen des Dokuments taucht in
  zwei Segmenten auf. Sonst würde dieselbe Stelle zweimal ersetzt und die
  zweite Ersetzung liefe auf bereits veränderten Text.
* Segmente ohne Textinhalt dürfen weggelassen werden.
* ``apply_replacements()`` mit leerem ``replacements``-Dict liefert ein
  Dokument, das inhaltlich identisch zum Original ist (Roundtrip-Garantie).
  Byte-Gleichheit ist **nicht** zugesichert — ein DOCX wird neu gepackt.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List

__all__ = ["Segment", "Replacement", "DocumentAdapter"]


@dataclass(frozen=True)
class Segment:
    """Ein zusammenhängender Textabschnitt eines Dokuments.

    Ein Segment ist die Einheit, auf der die Erkennung läuft. Es sollte gross
    genug sein, dass Kontext-abhängige Erkenner (spaCy-NER) arbeiten können —
    ein Segment pro Wort wäre nutzlos — und klein genug, dass es einer
    eindeutigen Stelle im Dokument entspricht.

    Attributes
    ----------
    id
        Stabile, innerhalb des Dokuments eindeutige Kennung. Sie ist der
        Schlüssel, unter dem ``apply_replacements()`` die Ersetzungen erwartet.
        Sie ist zusätzlich *sprechend* gestaltet (z. B. ``"body/p3"`` oder
        ``"fallback:word/footnotes.xml/p2"``), damit sich in Logs und Tests
        ablesen lässt, woher ein Treffer stammt. Aufrufer dürfen die ID
        trotzdem nur als undurchsichtigen Schlüssel behandeln.
    text
        Der reine Text des Abschnitts. **Alle** Offsets in :class:`Replacement`
        beziehen sich auf genau diesen String, gemessen in Python-Zeichen
        (nicht Bytes, nicht UTF-16-Codeunits).
    location
        Adresse, mit der der Adapter die Stelle beim Zurückschreiben wieder
        findet. Für Aufrufer **opak**: nicht interpretieren, nicht verändern,
        aber unverändert weiterreichen, falls Segmente serialisiert werden.
    """

    id: str
    text: str
    location: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class Replacement:
    """Eine einzelne Ersetzung innerhalb eines Segments.

    Attributes
    ----------
    start
        Start-Offset (inklusiv) in ``Segment.text``.
    end
        End-Offset (exklusiv) in ``Segment.text``. ``start == end`` ist eine
        reine Einfügung an dieser Position.
    text
        Der einzusetzende Text, typischerweise ein Platzhalter wie
        ``"[[PERSON_1]]"``. Leerstring bedeutet Löschung.

    Notes
    -----
    Ersetzungen einer Liste dürfen sich **nicht überlappen**. Der Adapter darf
    das prüfen und mit ``ValueError`` abbrechen — stillschweigend die eine oder
    andere zu verwerfen wäre schlimmer, weil dann Personendaten unbemerkt im
    Dokument bleiben.

    Die Reihenfolge in der Liste ist egal: Der Adapter sortiert selbst und
    wendet **von rechts nach links** an, damit frühere Ersetzungen die Offsets
    der späteren nicht verschieben.
    """

    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        """Prüft die Offsets grob auf Plausibilität."""
        if self.start < 0:
            raise ValueError(f"Negativer Start-Offset: {self.start}")
        if self.end < self.start:
            raise ValueError(
                f"End-Offset {self.end} liegt vor Start-Offset {self.start}"
            )


class DocumentAdapter(abc.ABC):
    """Abstrakte Basisklasse für alle Dokument-Adapter.

    Eine ABC statt eines ``typing.Protocol``: Die Adapter dieses Projekts
    erben tatsächlich voneinander (``CsvAdapter`` von ``TextAdapter``) und
    profitieren davon, dass vergessene Methoden schon beim Instanzieren
    auffallen statt erst beim Aufruf.

    Implementierungen sind zustandslos und damit thread-safe; eine Instanz darf
    beliebig oft und nebenläufig benutzt werden.
    """

    #: MIME-Typen, die dieser Adapter bedient. Wird von der Registry in
    #: ``backend/adapters/__init__.py`` ausgewertet.
    content_types: tuple[str, ...] = ()

    #: Dateiendungen (mit Punkt, kleingeschrieben), die dieser Adapter bedient.
    extensions: tuple[str, ...] = ()

    @abc.abstractmethod
    def extract_segments(self, data: bytes) -> List[Segment]:
        """Zerlegt das Dokument in überlappungsfreie Textsegmente.

        Parameters
        ----------
        data
            Die vollständigen Bytes des Dokuments.

        Returns
        -------
        list of Segment
            In Lesereihenfolge, mit eindeutigen IDs. Leere Segmente dürfen
            fehlen; die Liste darf leer sein.

        Raises
        ------
        ValueError
            Wenn ``data`` nicht dem erwarteten Format entspricht.
        """

    @abc.abstractmethod
    def apply_replacements(
        self,
        data: bytes,
        replacements: Dict[str, List[Replacement]],
    ) -> bytes:
        """Schreibt Ersetzungen formaterhaltend in das Dokument zurück.

        Parameters
        ----------
        data
            Dieselben Bytes, die zuvor an :meth:`extract_segments` gingen.
            Werden andere Bytes übergeben, sind die Locations bedeutungslos —
            der Adapter kann das nicht zuverlässig erkennen.
        replacements
            Abbildung ``Segment.id`` → Liste von :class:`Replacement`.
            IDs, die im Dokument nicht vorkommen, werden ignoriert (ein
            Segment kann durch eine vorgelagerte Filterung entfallen sein).
            Segmente ohne Eintrag bleiben unverändert.

        Returns
        -------
        bytes
            Das neue Dokument. Formatierung, Formeln, Bilder, Nummerierung und
            alle nicht berührten Teile bleiben erhalten.

        Raises
        ------
        ValueError
            Bei überlappenden Ersetzungen oder Offsets ausserhalb des
            Segmenttexts.
        """

    @abc.abstractmethod
    def strip_metadata(self, data: bytes) -> bytes:
        """Entfernt personenbezogene Metadaten aus dem Dokument.

        Metadaten sind der klassische blinde Fleck: Der Fliesstext ist sauber
        pseudonymisiert, aber ``docProps/core.xml`` nennt weiterhin Autor und
        Firma. Diese Methode leert solche Felder.

        Formate ohne Metadaten (Text, CSV) geben ``data`` unverändert zurück.

        Parameters
        ----------
        data
            Die vollständigen Bytes des Dokuments.

        Returns
        -------
        bytes
            Das Dokument ohne die betroffenen Metadatenfelder. Die Felder
            werden geleert, nicht gelöscht — manche Programme erwarten sie.
        """
