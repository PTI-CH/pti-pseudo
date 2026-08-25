"""Verwirft NER-Funde, die das Modell selbst als Gattungswort führt.

Das Problem, gemessen
---------------------
Das NER-Modell meldet einzelne, grossgeschriebene Substantive als Entitäten:
``Kontrollschild`` als ORGANIZATION, ``Fallnummer`` als LOCATION,
``Lagebericht`` als PERSON. Im Golden-Set stehen diese Fehlalarme seit dem
ersten Messlauf als „Schwäche 3".

Mit den Formaten XLSX und PPTX wiegt sie plötzlich schwer. Im Fliesstext ist
so ein Wort von einem Satz umgeben und das Modell ordnet es meist richtig ein
— eine Tabellenüberschrift ist dagegen **ein Wort**, ein Folientitel ebenso.
Genau dort trifft die Schwäche also am häufigsten, und genau dort war die
Precision unter die Zielmarke gefallen.

Die Regel
---------
Ein Fund wird verworfen, wenn **alle** vier Bedingungen zutreffen:

1. Er stammt vom **spaCy-Erkenner**. Ein Treffer der Begriffsliste ist eine
   ausdrückliche Anordnung eines Menschen und wird nie angetastet; Muster mit
   Prüfziffer (AHV, IBAN) haben mit Wortarten ohnehin nichts zu tun.
2. Sein Typ ist ``PERSON`` — **nur dieser**. Siehe unten.
3. Er umfasst **genau ein Token**. Mehrwortfunde bleiben unberührt — bei
   ``Sandra Meier`` ist die Sache klar, und dort liegt auch der Grossteil der
   echten Namen.
4. Dieses Token führt das Modell **nicht** als Eigennamen (``PROPN``).

Warum nur ``PERSON``
--------------------
Die erste Fassung galt für alle drei NER-Typen. Der Golden-Set-Lauf hat sie
widerlegt: Die Precision stieg auf 98.1 %, der **Recall fiel auf 85.2 %**.
Verloren gingen ``Talblick`` (eine Firma, sechsmal) und ``Bahnhofstrasse``.

Der Grund ist nicht Zufall, sondern Sprache. Organisations- und Ortsnamen
*sind* im Deutschen überwiegend aus gewöhnlichen Wörtern gebaut —
``Talblick``, ``Bahnhofstrasse``, ``Bergsicht``, ``Sonnenhof``. Das Modell
führt sie als ``NOUN``, und es hat damit nicht einmal unrecht. Bei
Personennamen ist der Anteil solcher Wörter viel kleiner, und die Ausnahmen
sind unten beziffert.

Das ist der Grund, warum diese Regel eng gefasst ist: Der Preis für die
zusätzlichen zwei verworfenen Fehlalarme wären sieben verlorene echte Funde
gewesen — in einem Werkzeug, das Personendaten entfernt, ein schlechtes
Geschäft.

Was die Regel kostet — nachgemessen, nicht geschätzt
-----------------------------------------------------
Der Einwand liegt auf der Hand: Viele deutsche Nachnamen *sind* Substantive.
Zwanzig davon (``Bauer``, ``Koch``, ``Sommer``, ``Jäger`` …) wurden am
2026-08-24 gegen das Modell geprüft:

* **Im Satz: 0 von 20 falsch getaggt.** ``Die Auskunft erteilte Frau Bauer.``
  → ``Bauer`` ist ``PROPN``. Im Fliesstext ist die Regel folgenlos.
* **Allein stehend (wie in einer Tabellenzelle): 6 von 20** werden als
  ``NOUN``/``ADV`` geführt. Aber: Von neun geprüften Wörtern liefert das
  NER-Modell für **acht überhaupt keinen Fund** — die Regel ist dort
  gegenstandslos, weil nichts zu verwerfen da ist. Diese Namen fehlen der
  Erkennung schon vorher; das ist eine bestehende Lücke, keine neue.

Übrig bleibt ein gemessener echter Verlust: ``Jäger`` allein in einer Zelle
wird als PERSON erkannt, als ``NOUN`` geführt und von dieser Regel verworfen.

Warum das vertretbar ist — und was den Rest auffängt
-----------------------------------------------------
:func:`backend.pipeline.propagate_findings` läuft **nach** dieser Regel. Wird
derselbe Name irgendwo im Dokument in einem Satz erkannt — und dort ist er
laut Messung ``PROPN`` —, trägt die Übertragung ihn an die kontextlose Stelle
nach. Der Verlust beschränkt sich damit auf den Fall, dass ein
Substantiv-Nachname im **ganzen** Dokument nur ein einziges Mal und nur ohne
Kontext vorkommt.

Diese Abwägung ist bewusst getroffen und gehört überprüft, wenn das Golden-Set
wächst: Wer sie umdrehen will, findet die Zahlen oben und den Waechter in
``golden-set/``.

Warum nicht auch ORT und ORGANISATION — die Tür ist zu
-------------------------------------------------------
Es liegt nahe, die Regel auf ``LOCATION`` und ``ORGANIZATION`` auszuweiten:
Die verbleibenden Fehlalarme des Golden-Sets sind fast alle Gattungswörter
(``Fallnummer``, ``Kontrollschild``, ``Polizei-Abfrageplattform``, ``Intern``).
Ein erster Versuch kostete sieben echte Funde und wurde zurückgenommen — was
wie ein Umsetzungsfehler aussah.

Am 2026-08-24 nachgemessen, und es ist keiner. Die Wortart trennt diese
beiden Gruppen schlicht nicht, **auch nicht im Satzzusammenhang**:

    echte Funde   Talblick/NOUN   Bahnhofstrasse/NOUN   Rütiweg/NOUN
    Fehlalarme    Fallnummer/NOUN Kontrollschild/NOUN   Polizei-…/NOUN

Deutsche Strassen- und Flurnamen *sind* Substantive; das Modell hat recht und
liefert trotzdem kein verwertbares Signal. Kein Feinschliff an dieser Regel
ändert daran etwas — wer diese Fehlerklasse angehen will, braucht die
KI-Schicht (``backend/ki/``) und nicht die Wortarten.
"""

from __future__ import annotations

import logging
from typing import Any, List, Sequence, Tuple

from presidio_analyzer import RecognizerResult

from backend.engine.entities import PERSON

logger = logging.getLogger("pti.engine.wortarten")

__all__ = ["BETROFFENE_TYPEN", "SPACY_RECOGNIZER", "verwerfe_gattungswoerter"]

#: Typen, für die die Regel gilt. Bewusst nur ``PERSON`` — die Begründung
#: steht im Modulkopf unter „Warum nur PERSON" und ist am Golden-Set gemessen.
BETROFFENE_TYPEN = frozenset({PERSON})

#: Name, unter dem der spaCy-Erkenner seine Treffer kennzeichnet. Presidio
#: legt ihn in ``recognition_metadata`` ab.
SPACY_RECOGNIZER = "SpacyRecognizer"

#: Wortart eines Eigennamens. Alles andere gilt als Gattungswort.
EIGENNAME = "PROPN"


def _ist_spacy_fund(ergebnis: RecognizerResult) -> bool:
    """Ob der Treffer vom NER-Modell stammt und nicht von Liste oder Muster."""
    metadaten = ergebnis.recognition_metadata or {}
    return metadaten.get(RecognizerResult.RECOGNIZER_NAME_KEY) == SPACY_RECOGNIZER


def _tokens_im_bereich(doc: Any, start: int, ende: int) -> List[Any]:
    r"""Die Tokens, die sich mit ``[start, ende)`` überschneiden.

    Leerraum-Tokens zählen nicht mit: Ein Fund über eine Zellgrenze hinweg
    (``Zwicky\tMarlene``) enthält den Tabulator als eigenes ``SPACE``-Token,
    ist aber trotzdem ein Zweiwortfund und darf nicht als Einwortfund gelten.
    """
    treffer = []
    for token in doc:
        if token.idx >= ende:
            break
        if token.idx + len(token.text) <= start:
            continue
        if token.is_space:
            continue
        treffer.append(token)
    return treffer


def verwerfe_gattungswoerter(
    ergebnisse: Sequence[RecognizerResult],
    doc: Any,
) -> Tuple[List[RecognizerResult], List[RecognizerResult]]:
    """Trennt die Funde in behaltene und verworfene.

    Parameters
    ----------
    ergebnisse
        Die Treffer eines Segments, wie Presidio sie liefert.
    doc
        Das spaCy-``Doc`` **desselben** Texts (``NlpArtifacts.tokens``). Die
        Offsets müssen zusammenpassen — deshalb stammt beides aus einem
        einzigen NLP-Durchlauf.

    Returns
    -------
    (behalten, verworfen)
        ``verworfen`` wird nicht weggeworfen, sondern zurückgegeben: Der
        Qualitätsbericht soll zeigen können, was diese Regel entfernt hat.
        Eine still wirkende Regel wäre in einem Werkzeug, das Personendaten
        entfernt, das Letzte, was man haben will.
    """
    if doc is None:
        # Ohne Wortarten keine Regel. Lieber alle Funde behalten als raten —
        # ein verworfener Name ist teurer als ein Fehlalarm.
        return list(ergebnisse), []

    behalten: List[RecognizerResult] = []
    verworfen: List[RecognizerResult] = []

    for ergebnis in ergebnisse:
        if ergebnis.entity_type not in BETROFFENE_TYPEN or not _ist_spacy_fund(
            ergebnis
        ):
            behalten.append(ergebnis)
            continue

        tokens = _tokens_im_bereich(doc, ergebnis.start, ergebnis.end)
        if len(tokens) != 1:
            behalten.append(ergebnis)
            continue

        if tokens[0].pos_ == EIGENNAME:
            behalten.append(ergebnis)
            continue

        logger.debug(
            "Gattungswort verworfen: %r als %s (Wortart %s)",
            tokens[0].text,
            ergebnis.entity_type,
            tokens[0].pos_,
        )
        verworfen.append(ergebnis)

    return behalten, verworfen
