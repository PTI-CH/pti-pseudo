"""Format der Ersatzsymbole (Pseudonyme) und deren Parser.

Warum ``[[PERSON_3]]`` und nicht "Person B"
-------------------------------------------
Das Fachkonzept schlug sprechende Ersatznamen der Form "Person B" vor. Das liest
sich im pseudonymisierten Dokument angenehmer, ist beim Rueckfuehren aber nicht
kollisionssicher:

* Ein Dokument, das "Person B" legitim im Fliesstext enthaelt ("... die als
  Person B bezeichnete Auskunftsperson ..."), wuerde beim ``reverse`` faelschlich
  durch einen Klarnamen ersetzt. Aus einer Anonymisierung wird so eine
  Falschaussage — und zwar eine, die niemand bemerkt.
* Der Buchstabenraum ist zudem klein; jenseits von "Person Z" braucht es
  ohnehin eine Regel.

Die doppelte Klammerform kommt in natuerlichem Text praktisch nicht vor und ist
mit einem einzigen regulaeren Ausdruck eindeutig erkennbar. Der Zaehler ist eine
Zahl, damit er ohne Sonderfall beliebig weit laeuft.

Das Format ist ueber ``PSEUDONYM_TEMPLATE`` (Settings/Env) konfigurierbar. Wer
es aendert, muss wissen: bereits gespeicherte Pseudonyme werden **nicht**
umgeschrieben. Das ist Absicht — sie stehen als Literal in ausgelieferten
Dokumenten, und ``reverse`` ersetzt genau die gespeicherten Zeichenketten.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

#: Vorgabe-Format. Platzhalter: ``{entity_type}`` und ``{counter}``.
DEFAULT_PSEUDONYM_TEMPLATE = "[[{entity_type}_{counter}]]"


def _template_from_settings() -> str:
    """Holt das Format aus ``backend.config``, sonst aus der Env, sonst Vorgabe.

    Import bewusst innerhalb der Funktion — siehe ``crypto._setting``.
    """
    try:
        from backend.config import get_settings

        value = getattr(get_settings(), "pseudonym_template", None)
        if value:
            return str(value)
    except Exception:  # pragma: no cover — nur ohne/mit defekter Konfigschicht
        pass
    return os.environ.get("PSEUDONYM_TEMPLATE") or DEFAULT_PSEUDONYM_TEMPLATE


def get_template() -> str:
    """Gibt das aktuell gueltige Pseudonym-Format zurueck."""
    template = _template_from_settings()
    if "{entity_type}" not in template or "{counter}" not in template:
        raise ValueError(
            "PSEUDONYM_TEMPLATE muss beide Platzhalter enthalten: "
            "{entity_type} und {counter}. Vorgabe: "
            f"{DEFAULT_PSEUDONYM_TEMPLATE}"
        )
    return template


def format_pseudonym(
    entity_type: str, counter: int, template: str | None = None
) -> str:
    """Baut das Ersatzsymbol, z. B. ``[[PERSON_3]]``.

    Der Entitaetstyp wird auf Grossbuchstaben normiert, damit Presidios
    ``PERSON``/``LOCATION`` und eigene Erkenner dasselbe Erscheinungsbild
    ergeben und der Parser mit einer einzigen Zeichenklasse auskommt.
    """
    tpl = template or get_template()
    return tpl.format(entity_type=str(entity_type).upper(), counter=int(counter))


@lru_cache(maxsize=32)
def _compiled(template: str) -> re.Pattern[str]:
    """Leitet den Parser-Regex aus dem Format ab.

    Der Regex wird aus derselben Vorlage erzeugt, aus der auch gebaut wird —
    damit koennen Erzeuger und Parser nicht auseinanderlaufen, wenn jemand das
    Format aendert. ``{entity_type}`` ist nicht-gierig, damit der Unterstrich vor
    dem Zaehler auch bei mehrteiligen Typen (``LOC_ORT``) richtig trennt.
    """
    pattern = re.escape(template)
    pattern = pattern.replace(
        re.escape("{entity_type}"), r"(?P<entity_type>[A-Z0-9_]+?)"
    )
    pattern = pattern.replace(re.escape("{counter}"), r"(?P<counter>\d+)")
    return re.compile(pattern)


def pseudonym_pattern(template: str | None = None) -> re.Pattern[str]:
    """Regex, der Pseudonyme im Fliesstext findet (fuer Diagnose und Pruefungen)."""
    return _compiled(template or get_template())


def parse_pseudonym(text: str, template: str | None = None) -> tuple[str, int] | None:
    """Zerlegt ein Pseudonym in ``(entity_type, counter)`` oder gibt ``None``.

    Wird gebraucht, um beim Anlegen eines neuen Eintrags den naechsten freien
    Zaehler zu bestimmen, ohne dafuer eine eigene Zaehlerspalte pflegen zu
    muessen, die mit den Pseudonymen auseinanderlaufen koennte.
    """
    match = _compiled(template or get_template()).fullmatch(text.strip())
    if match is None:
        return None
    return match.group("entity_type"), int(match.group("counter"))
