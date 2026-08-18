"""Auflösung überlappender Treffer (Merge-/Overlap-Regeln).

Warum es diese Datei gibt
-------------------------
Presidio lässt jeden Erkenner unabhängig über denselben Text laufen und gibt
alle Treffer roh zurück. Bei einer einzigen E-Mail-Adresse im Testsatz waren
das real gemessen **vier** Treffer auf demselben Textstück:

===============================  =====  ==============================
Span                             Score  Typ
===============================  =====  ==============================
``andreas.meier@pti.ch``         1.00   ``EMAIL_ADDRESS``
``andreas.meier@pti.ch``         0.85   ``PERSON``   (spaCy-NER)
``andreas.me``                   0.50   ``URL``      (Teilstück!)
``pti.ch``                       0.50   ``URL``      (Teilstück!)
===============================  =====  ==============================

Ungefiltert weitergereicht würde die Anonymisierung dasselbe Textstück
mehrfach und teils überlappend ersetzen — das Ergebnis wäre Zeichensalat.
:func:`merge_overlaps` räumt das auf.

Die drei Regeln
---------------
1. **Überlappung → höherer Score gewinnt.** Der Erkenner, der sich sicherer
   ist, behält den Text. Im Beispiel schlägt ``EMAIL_ADDRESS`` (1.00) sowohl
   ``PERSON`` (0.85) als auch die beiden ``URL``-Fragmente (0.50).
2. **Gleichstand → längerer Span gewinnt.** Bei identischem Score ist der
   umfassendere Treffer der informativere. Typischer Fall: ``ORGANIZATION``
   „Kantonspolizei Zürich" (0.85) gegen ``LOCATION`` „Zürich" (0.85) — wir
   wollen die Organisation, nicht den Ort darin.
3. **Vollständig enthaltene Treffer entfallen.** Ein Treffer, der komplett in
   einem angenommenen Treffer liegt, wird ersatzlos gestrichen, nicht
   gekürzt. Genau das entsorgt die URL-Fragmente ``andreas.me`` und
   ``pti.ch``. Umgesetzt ist das über dieselbe Überlappungsprüfung wie
   Regel 1: Enthaltensein ist der Spezialfall vollständiger Überlappung.

Umsetzung
---------
Ein gieriges Verfahren (*greedy interval selection*): alle Kandidaten werden
nach einem Prioritätsschlüssel sortiert und in dieser Reihenfolge angenommen,
sofern sie sich nicht mit einem bereits angenommenen Treffer überschneiden.
Weil der Schlüssel die Regeln 1 und 2 direkt abbildet, gewinnt immer der
richtige Treffer, und Regel 3 fällt automatisch heraus.

Der Schlüssel ist **total** — er enthält Kriterien bis hinunter zum
Entitätsnamen —, damit das Ergebnis unabhängig von der Eingabereihenfolge und
zwischen zwei Läufen identisch ist. Das ist keine Kosmetik: die Pseudonyme im
Vault hängen an den Spans, ein wackelndes Ergebnis erzeugte Karteileichen.

Randfälle
---------
* **Berührende Spans** (``a.end == b.start``) überlappen *nicht* und bleiben
  beide erhalten. ``RecognizerResult.intersects`` liefert dafür 0.
* **Identische Spans mit identischem Score** (z. B. ``CH_IBAN`` und
  ``IBAN_CODE`` auf derselben Nummer — beide validieren über Mod-97 und landen
  deshalb beide bei 1.0) werden über :data:`PRIORITY_ENTITIES` entschieden,
  ersatzweise alphabetisch. Fachlich beliebig, aber stabil.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Sequence, Tuple

from presidio_analyzer import RecognizerResult

from backend.engine import entities

logger = logging.getLogger("pti.engine.merge")

#: Entitätstypen, die bei sonst völligem Gleichstand (gleicher Span, gleicher
#: Score) bevorzugt werden. Vorne = höhere Priorität.
#:
#: Der einzige real auftretende Fall: unser spezifischer ``CH_IBAN`` gegen
#: Presidios generischen ``IBAN_CODE``. Ohne diese Liste entschiede die
#: Alphabetik zugunsten von ``CH_IBAN`` — das ist zwar zufällig richtig, aber
#: wir wollen es nicht dem Zufall überlassen.
PRIORITY_ENTITIES: Tuple[str, ...] = (
    entities.CH_AHV,
    entities.CH_IBAN,
    entities.CH_PHONE,
    entities.EMAIL_ADDRESS,
    entities.CREDIT_CARD,
    entities.IBAN_CODE,
)


def _priority_rank(entity_type: str) -> int:
    """Rang aus :data:`PRIORITY_ENTITIES`; alles Unbekannte landet dahinter."""
    try:
        return PRIORITY_ENTITIES.index(entity_type)
    except ValueError:
        return len(PRIORITY_ENTITIES)


def _sort_key(result: RecognizerResult) -> Tuple[float, int, int, int, str]:
    """Prioritätsschlüssel eines Treffers — kleiner ist besser.

    Die Bestandteile in der Reihenfolge ihrer Wirkung:

    1. ``-score``      → Regel 1: höherer Score zuerst.
    2. ``-länge``      → Regel 2: bei Gleichstand der längere Span zuerst.
    3. ``priorität``   → gesteuerter Stichentscheid, :data:`PRIORITY_ENTITIES`.
    4. ``start``       → Textreihenfolge, rein zur Determinismus-Sicherung.
    5. ``entity_type`` → letzter, immer entscheidender Stichentscheid.

    Erst Punkt 4 und 5 machen den Schlüssel total: ohne sie hinge das Ergebnis
    bei exakt gleichwertigen Treffern an der Sortierstabilität und damit an
    der Reihenfolge, in der Presidio seine Erkenner registriert hat.
    """
    return (
        -float(result.score),
        -(result.end - result.start),
        _priority_rank(result.entity_type),
        result.start,
        result.entity_type,
    )


def _overlaps(a: RecognizerResult, b: RecognizerResult) -> bool:
    """Prüfen, ob sich zwei Treffer um mindestens ein Zeichen überschneiden.

    ``RecognizerResult.intersects`` liefert die Anzahl gemeinsamer Zeichen und
    damit 0 für berührende Spans — genau das gewünschte Verhalten.
    """
    return a.intersects(b) > 0


def merge_overlaps(results: Sequence[RecognizerResult]) -> List[RecognizerResult]:
    """Überlappende Treffer nach den oben beschriebenen Regeln auflösen.

    :param results: Rohe Trefferliste, wie sie ``AnalyzerEngine.analyze``
        liefert. Wird nicht verändert.
    :return: Neue Liste, in der sich kein Treffer mehr mit einem anderen
        überschneidet, sortiert nach Textposition (``start``, dann ``end``).

    Der Rückgabewert ist nach Textposition sortiert und nicht mehr nach
    Priorität: alles, was danach kommt (Anonymisierung, Vault, Anzeige),
    arbeitet den Text von vorne nach hinten ab.
    """
    if not results:
        return []

    accepted: List[RecognizerResult] = []
    for candidate in sorted(results, key=_sort_key):
        # Regeln 1-3 in einer Prüfung: Der Kandidat kommt nur durch, wenn kein
        # bereits angenommener — und damit nach Schlüssel *besserer* — Treffer
        # denselben Text beansprucht. Ob der Kandidat teilweise überlappt oder
        # vollständig enthalten ist, spielt dabei keine Rolle; beides führt
        # zum ersatzlosen Verwerfen.
        conflict = next((a for a in accepted if _overlaps(a, candidate)), None)
        if conflict is not None:
            logger.debug(
                "Treffer verworfen: %s [%d:%d] score=%.2f — verdrängt von "
                "%s [%d:%d] score=%.2f",
                candidate.entity_type,
                candidate.start,
                candidate.end,
                candidate.score,
                conflict.entity_type,
                conflict.start,
                conflict.end,
                conflict.score,
            )
            continue
        accepted.append(candidate)

    accepted.sort(key=lambda r: (r.start, r.end, r.entity_type))
    return accepted


def _dropped(
    results: Iterable[RecognizerResult], kept: Sequence[RecognizerResult]
) -> List[RecognizerResult]:
    """Alle Treffer, die :func:`merge_overlaps` aussortiert hat."""
    kept_ids = {id(k) for k in kept}
    return [r for r in results if id(r) not in kept_ids]


def describe_conflicts(results: Sequence[RecognizerResult]) -> List[str]:
    """Menschenlesbare Begründung, welcher Treffer wen verdrängt hat.

    Reines Diagnosewerkzeug für Tests und den Debug-Modus der Oberfläche.
    Verändert nichts und wird im Normalbetrieb nicht aufgerufen.
    """
    kept = merge_overlaps(results)
    lines: List[str] = []
    for dropped in _dropped(results, kept):
        winner = next((k for k in kept if _overlaps(k, dropped)), None)
        if winner is None:
            lines.append(
                f"{dropped.entity_type} [{dropped.start}:{dropped.end}] "
                f"score={dropped.score:.2f} — Duplikat"
            )
            continue
        lines.append(
            f"{dropped.entity_type} [{dropped.start}:{dropped.end}] "
            f"score={dropped.score:.2f} verdrängt von {winner.entity_type} "
            f"[{winner.start}:{winner.end}] score={winner.score:.2f}"
        )
    return lines
