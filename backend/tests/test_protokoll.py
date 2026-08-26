"""Die Protokollkonfiguration — und warum es sie ueberhaupt braucht.

Am 2026-08-25 auf dem Produktionsknoten gemessen: Die Anwendung konfigurierte
ihr Protokoll gar nicht. `pti.*` fiel damit auf den Wurzel-Logger zurueck,
dessen Vorgabe WARNING ist, und Pythons `lastResort`-Behandler gab nur WARNING
und hoeher aus. In `/home/jelastic/log/pythonengine.log` stand deshalb
`logger.warning("Schema ergaenzt…")`, aber `logger.info("Erkennungs-Engine
geladen…")` **null Mal**.

Das war kein Schoenheitsfehler. Die Messung zu Ort und Organisation
(`pipeline._ki_messen_ort_organisation`) schreibt ihre Ergebnisse mit
`logger.info` — sie hat seit ihrem Deploy **nichts gesammelt**, obwohl der
Auftraggeber sie ausdruecklich als „erst mitmessen, dann entscheiden" gewaehlt
hatte. Ein Messwerkzeug, dessen Messwerte nirgends landen, ist schlimmer als
keines: Es erzeugt die Erwartung, spaeter Zahlen zu haben.
"""

from __future__ import annotations

import logging

import pytest

from backend.main import (
    BEHANDLER_NAME,
    PROTOKOLL_NAMENSRAUM,
    richte_protokoll_ein,
)


def _eigene(logger: logging.Logger) -> list[logging.Handler]:
    """Nur die Behandler dieser Anwendung — nicht die der Testumgebung."""
    return [h for h in logger.handlers if getattr(h, "name", None) == BEHANDLER_NAME]


@pytest.fixture(autouse=True)
def _sauberer_logger():
    """Nimmt die Konfiguration nach jedem Test zurueck.

    Ohne das truege ein Test die Behandler des vorigen mit sich — und die
    Zusicherung „genau ein Behandler" pruefte dann den Nachbarn.
    """
    logger = logging.getLogger(PROTOKOLL_NAMENSRAUM)
    vorher = list(logger.handlers), logger.level, logger.propagate
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    logger.handlers.extend(vorher[0])
    logger.level, logger.propagate = vorher[1], vorher[2]


def test_info_wird_ausgegeben():
    """Der Kern: INFO muss ankommen, sonst sammelt die Messung nichts."""
    richte_protokoll_ein()
    logger = logging.getLogger(PROTOKOLL_NAMENSRAUM)

    assert logger.isEnabledFor(logging.INFO)
    assert _eigene(logger), "ohne Behandler greift Pythons lastResort — und der kann nur WARNING"


def test_die_stufe_laesst_sich_ueber_die_umgebung_senken(monkeypatch):
    """Abstellbar ohne Deploy, wie der KI-Schalter auch."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        richte_protokoll_ein()
        logger = logging.getLogger(PROTOKOLL_NAMENSRAUM)
        assert not logger.isEnabledFor(logging.INFO)
        assert logger.isEnabledFor(logging.WARNING)
    finally:
        get_settings.cache_clear()


def test_zweimaliges_einrichten_haengt_keinen_zweiten_behandler_an():
    """Bei zwei uvicorn-Workern laeuft `create_app` zweimal im selben Prozess.

    Ohne diese Zusicherung stuende jede Zeile doppelt im Protokoll — und
    niemand kaeme darauf, dass die Konfiguration schuld ist.
    """
    richte_protokoll_ein()
    richte_protokoll_ein()

    # Gezaehlt wird der EIGENE Behandler: Unter pytest haengt die Testumgebung
    # ihre eigenen an denselben Logger, und die zaehlen hier nicht mit.
    assert len(_eigene(logging.getLogger(PROTOKOLL_NAMENSRAUM))) == 1


def test_jede_meldung_traegt_stufe_und_herkunft():
    """Ohne beides ist eine Zeile im Protokoll nicht zuzuordnen.

    Genau daran ist die Untersuchung am 2026-08-25 fast gescheitert: „Schema
    ergaenzt" stand nackt da, ohne Stufe und ohne Herkunft, und liess sich
    nicht von einer Zeile des Webservers unterscheiden.

    Geprueft wird der Behandler selbst statt der Standardausgabe: Unter pytest
    faengt die Testumgebung Protokoll und Ausgabe getrennt ab, und ein Test
    gegen `capsys` pruefte dann die Abfangvorrichtung statt unser Format.
    """
    richte_protokoll_ein()
    (behandler,) = _eigene(logging.getLogger(PROTOKOLL_NAMENSRAUM))

    zeile = behandler.format(
        logging.LogRecord(
            "pti.jobs", logging.INFO, __file__, 1,
            "Auftrag angelegt: %s, %d Byte", ("anonymize", 42), None,
        )
    )
    assert zeile == "INFO [pti.jobs] Auftrag angelegt: anonymize, 42 Byte"


def test_alle_logger_des_baums_liegen_im_namensraum():
    """Eine Konfiguration erreicht nur, was unter ihrem Namen haengt.

    Am 2026-08-25 hiessen zwei der vierzehn Logger `backend.*` statt `pti.*`
    (`main.py` und `core/api_key_auth.py`, beide ueber `__name__`). Sie waeren
    von der Konfiguration nicht erfasst worden — und ausgerechnet in `main.py`
    stehen die Startmeldungen, an denen man erkennt, ob der Auftragsarbeiter
    laeuft.
    """
    import pathlib
    import re

    wurzel = pathlib.Path(__file__).resolve().parent.parent
    fremde = []
    for datei in wurzel.rglob("*.py"):
        if "tests" in datei.parts:
            continue
        for treffer in re.finditer(r"getLogger\(([^)]*)\)", datei.read_text(encoding="utf-8")):
            name = treffer.group(1).strip()
            # Die Konstante selbst ist kein Verstoss — sie IST der Namensraum.
            if name == "PROTOKOLL_NAMENSRAUM":
                continue
            if not name.startswith(f'"{PROTOKOLL_NAMENSRAUM}.'):
                fremde.append(f"{datei.relative_to(wurzel)}: {name}")

    assert not fremde, "Logger ausserhalb des konfigurierten Namensraums: " + ", ".join(fremde)


def test_die_anwendung_richtet_das_protokoll_beim_bau_ein(monkeypatch):
    """Der eigentliche Fehler von 2026-08-25 war nicht eine falsche Stufe.

    Es gab die Konfiguration schlicht nirgends. Ein Test, der nur
    `richte_protokoll_ein` prueft, bliebe deshalb gruen, waehrend niemand die
    Funktion aufruft — genau der Zustand, der ein halbes Jahr Messwerte
    gekostet haette. Diese Zusicherung prueft die Verdrahtung selbst.
    """
    for name, wert in (
        ("HUB_AUTH", "off"), ("ENV", "development"),
        ("DATABASE_URL", "sqlite://"), ("VAULT_MASTER_KEY", "d" * 64),
    ):
        monkeypatch.setenv(name, wert)

    from backend.config import get_settings
    from backend.main import create_app

    get_settings.cache_clear()
    try:
        create_app()
        assert _eigene(logging.getLogger(PROTOKOLL_NAMENSRAUM))
    finally:
        get_settings.cache_clear()
