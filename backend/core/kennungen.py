"""Das Umwandeln einer Zeichenkette in eine UUID — an einer Stelle.

Vier Router (``documents``, ``jobs``, ``vaults``, ``api_keys``) und zwei
weitere Stellen (``terms``, ``jobs.ausfuehrung``) hatten je eine eigene
Fassung derselben sechs Zeilen. Der Unterschied zwischen ihnen war nie die
Umwandlung, sondern immer nur, WAS bei Unsinn geschehen soll: 404 hier, 400
dort, ein ``ValueError`` im Auftragspfad, der in keiner Anfrage laeuft.

Genau so ist es hier geschnitten: Diese Funktion wandelt um und meldet
Misserfolg mit ``None``. Die Bedeutung von Misserfolg bleibt beim Aufrufer.

Warum das mehr als Ordnungsliebe ist: Die Liste der abzufangenden Ausnahmen
(``ValueError``, ``AttributeError``, ``TypeError``) ist nicht offensichtlich —
``uuid.UUID(None)`` wirft ``AttributeError``, nicht ``ValueError``, und
``uuid.UUID(42)`` wirft ``TypeError``. In sechs Kopien haette eine davon
frueher oder spaeter eine Ausnahme zu wenig gefangen, und aus einer sauberen
404 waere eine 500 geworden.
"""

from __future__ import annotations

import uuid


def als_uuid(wert: object) -> uuid.UUID | None:
    """Wandelt ``wert`` in eine ``UUID`` um, oder gibt ``None`` zurueck.

    Nimmt bewusst ``object`` und nicht ``str``: Die Aufrufer bekommen ihre
    Werte aus Pfadangaben, Formularfeldern und JSON-Spalten, wo neben einer
    Zeichenkette auch ``None``, eine Zahl oder eine Liste ankommen kann.

    >>> als_uuid("00000000-0000-0000-0000-000000000000") is None
    False
    >>> als_uuid("kein-uuid") is None
    True
    >>> als_uuid(None) is None
    True
    """
    try:
        return uuid.UUID(str(wert))
    except (ValueError, AttributeError, TypeError):
        return None
