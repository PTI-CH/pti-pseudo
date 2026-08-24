"""Haelt fest, dass die KI-Einstellungen vollstaendig ankommen.

Der Anlass ist eine Falle, die zweimal zugeschnappt waere:

1. ``INFOMANIAK_AI_KEY`` stand in ``config.py`` und ``.env.example``, fehlte
   aber in der Export-Liste von ``deploy/pythonenv``. Der Runner laedt
   ``/.jelenv`` per ``source``, exportiert die Werte aber nicht — nur
   namentlich aufgefuehrte Variablen erreichen den Prozess. Ein im Dashboard
   gesetzter Schluessel waere lautlos verschwunden.
2. ``INFOMANIAK_AI_PRODUCT_ID`` fehlte ganz. Sie steht im **Pfad** der Anfrage
   (``{base}/2/ai/{produkt}/openai/v1/chat/completions``); ohne sie laesst
   sich der Dienst nicht adressieren, auch mit gueltigem Token nicht.

Beide Fehler haetten dasselbe Bild erzeugt: Der Token ist gesetzt, und die
KI-Schicht funktioniert trotzdem nicht — ohne dass irgendwo etwas danebengeht,
das man suchen koennte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Variablen, die Phase 3 braucht. ``BASE`` und ``MODELL`` haben brauchbare
#: Vorgaben und muessen nicht gesetzt werden — exportiert gehoeren sie
#: trotzdem, sonst laesst sich die Vorgabe am Knoten nicht uebersteuern.
KI_VARIABLEN = (
    "INFOMANIAK_AI_KEY",
    "INFOMANIAK_AI_PRODUCT_ID",
    "INFOMANIAK_AI_BASE",
    "INFOMANIAK_AI_MODELL",
)


@pytest.mark.parametrize("name", KI_VARIABLEN)
def test_runner_exportiert_die_ki_variablen(name: str) -> None:
    """Was ``deploy/pythonenv`` nicht exportiert, sieht die Anwendung nie."""
    inhalt = (_REPO_ROOT / "deploy" / "pythonenv").read_text(encoding="utf-8")
    export_zeilen = [z for z in inhalt.splitlines() if z.startswith("export ")]
    exportiert = {wort for zeile in export_zeilen for wort in zeile.split()[1:]}
    assert name in exportiert, (
        f"{name} fehlt in der Export-Liste von deploy/pythonenv. "
        "Ein im Dashboard gesetzter Wert erreicht den Prozess dann nicht."
    )


@pytest.mark.parametrize("name", KI_VARIABLEN)
def test_einstellung_existiert(name: str) -> None:
    """Zu jeder Variablen gehoert ein Feld in ``Settings``."""
    from backend.config import Settings

    assert name.lower() in Settings.model_fields, (
        f"{name} hat kein Feld in backend/config.py — der Wert kaeme im Prozess "
        "an und wuerde von pydantic-settings verworfen."
    )


def test_produkt_id_hat_eine_leere_vorgabe() -> None:
    """Kein geratener Wert.

    Eine erfundene Produkt-Id ergaebe eine 404 gegen einen fremden Pfad statt
    einer klaren Meldung „nicht konfiguriert".
    """
    from backend.config import Settings

    assert Settings.model_fields["infomaniak_ai_product_id"].default == ""


def test_env_example_nennt_beide_pflichtwerte() -> None:
    """Die Datei ist die verbindliche Liste — sie darf nicht die Haelfte nennen."""
    inhalt = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("INFOMANIAK_AI_KEY", "INFOMANIAK_AI_PRODUCT_ID"):
        assert name in inhalt, f"{name} fehlt in .env.example"
