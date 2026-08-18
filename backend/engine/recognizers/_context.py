r"""Hartes Kontext-Gating für mehrdeutige Muster.

Warum das nötig ist
-------------------
Presidio kennt zwar einen Kontextmechanismus (``context=[…]`` am Erkenner plus
``LemmaContextAwareEnhancer``), aber der kann nur **anheben**: passt der
Kontext, steigt der Score; passt er nicht, bleibt der Treffer unverändert
bestehen. Für ``\b[1-9]\d{3}\b`` — die Schweizer PLZ — reicht das nicht.
Dieses Muster trifft jede Jahreszahl, jeden vierstelligen Betrag und jedes
Aktenzeichen. Ohne Kontext darf es schlicht **gar nicht** feuern.

Deshalb dieser Baustein: eine Prüfung, die vor der Rückgabe eines Treffers
verlangt, dass in einem Fenster um die Fundstelle ein Kontextwort steht.
Presidios ``context``-Liste bleibt zusätzlich gesetzt — sie hebt den Score des
überlebenden Treffers an. Die beiden Mechanismen ergänzen sich:
Gate (hier) = *ob überhaupt*, Enhancer (Presidio) = *wie sicher*.

Normalisierung
--------------
Der Vergleich läuft über kleingeschriebenen, umlautgefalteten Text
(``ä→ae``, ``ö→oe``, ``ü→ue``, ``ß→ss``). Damit trifft das Kontextwort
``zustelladresse`` auch die Schreibweise ``Zustelladresse`` und ``strasse``
auch ``Straße``.

Wortgrenzen
-----------
Geprüft wird nur die **rechte** Wortgrenze, nicht die linke. Das ist Absicht:
``strasse`` soll auch in ``Bahnhofstrasse`` treffen und ``adresse`` auch in
``Zustelladresse`` — genau die Komposita, in denen deutsche Adressangaben
stecken. Die linke Grenze wegzulassen kostet hier nichts, weil die
Kontextwörter selbst schon spezifisch sind.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import regex as re

#: Grösse des Kontextfensters in Zeichen, jeweils links und rechts der
#: Fundstelle. 60 Zeichen ≈ eine Zeile Fliesstext. Grösser gewählt zieht
#: Kontext aus dem Nachbarsatz herein, kleiner reisst „Postleitzahl: 8001"
#: mit vorangestelltem Label auseinander.
DEFAULT_WINDOW = 60

#: Umlautfaltung. Reihenfolge egal, die Ersetzungen überschneiden sich nicht.
_FOLDING = (
    ("ä", "ae"),
    ("ö", "oe"),
    ("ü", "ue"),
    ("ß", "ss"),
)


def normalize(text: str) -> str:
    """Text kleinschreiben und Umlaute falten.

    Bewusst *keine* Unicode-NFKD-Zerlegung: die würde ``ü`` zu ``u`` machen und
    damit ``Zurich`` und ``Zürich`` gleichsetzen — für Kontextwörter egal, für
    die spätere Begriffsliste aber zu grob.
    """
    lowered = text.lower()
    for umlaut, replacement in _FOLDING:
        lowered = lowered.replace(umlaut, replacement)
    return lowered


def _context_pattern(words: Iterable[str]) -> "re.Pattern[str]":
    """Ein Alternativ-Regex über alle Kontextwörter bauen (bereits normalisiert)."""
    alternatives = "|".join(re.escape(normalize(word)) for word in words)
    # Nur rechte Grenze — siehe Modul-Docstring.
    return re.compile(rf"(?:{alternatives})(?![a-z0-9])")


def has_context(
    text: str,
    start: int,
    end: int,
    context_words: Sequence[str],
    window: int = DEFAULT_WINDOW,
) -> bool:
    """Prüfen, ob im Fenster um ``[start:end]`` ein Kontextwort steht.

    :param text: Der vollständige analysierte Text.
    :param start: Startindex des Treffers.
    :param end: Endindex des Treffers (exklusiv).
    :param context_words: Die Kontextwörter des Erkenners.
    :param window: Fenstergrösse in Zeichen je Seite.
    :return: ``True``, wenn der Treffer durchgelassen werden darf.

    Der Treffer selbst wird aus dem Fenster **herausgeschnitten**. Sonst könnte
    ein Treffer sich selbst legitimieren — etwa ein Kontrollschild ``ZG 4711``,
    dessen Kantonskürzel zufällig in einem Kontextwort vorkäme.
    """
    if not context_words:
        return True

    left = text[max(0, start - window) : start]
    right = text[end : end + window]
    haystack = normalize(left) + "\n" + normalize(right)
    return _context_pattern(context_words).search(haystack) is not None


def preceded_by(
    text: str, start: int, tokens: Sequence[str], lookback: int = 12
) -> bool:
    """Prüfen, ob unmittelbar links vor ``start`` einer der Marker steht.

    Gedacht als Negativfilter: ``CHF 8500`` ist keine Postleitzahl, ``Art. 1234``
    kein Wohnort. Anders als :func:`has_context` schaut diese Prüfung nur ein
    kurzes Stück zurück und verlangt, dass der Marker direkt (nur durch
    Leerzeichen oder Satzzeichen getrennt) vor dem Treffer steht.
    """
    left = normalize(text[max(0, start - lookback) : start])
    for token in tokens:
        marker = normalize(token)
        if re.search(rf"(?:^|[^a-z0-9]){re.escape(marker)}[^a-z0-9]*$", left):
            return True
    return False
