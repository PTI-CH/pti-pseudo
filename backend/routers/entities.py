"""Katalog-Endpunkt: welche Entitaetstypen kennt die Engine.

Liefert dieselben Stammdaten wie ``backend/engine/entities.py``, aber als
JSON — die Oberflaeche braucht das fuer die Kategorienwahl (welche Haekchen
gibt es, was ist vorausgewaehlt) und soll die Liste nicht im Frontend
duplizieren.

Der Router traegt kein eigenes Praefix: ``backend/main.py`` haengt ihn zweimal
ein, unter ``/api/v1`` fuer die Oberflaeche und unter ``/api/ext/v1`` fuer
aufrufende Anwendungen. Der Katalog ist damit auf beiden Wegen dieselbe
Wahrheit.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.engine import entities as ents

router = APIRouter(tags=["entities"])


@router.get("/entities")
async def list_entities() -> dict:
    """Vollstaendiger Katalog aller Entitaetstypen in Anzeigereihenfolge.

    ``source`` unterscheidet, woher ein Treffer stammen kann: die PTI-eigenen
    Typen (``PTI_PROJEKT``, ``PTI_PRODUKT``) kommen ausschliesslich ueber die
    Begriffsliste herein, kein Erkenner findet sie von selbst (siehe
    ``ents.NUR_BEGRIFFSLISTE``). Die Oberflaeche kann damit z. B. erklaeren,
    warum ein frisch angelegtes PTI-Projekt erst nach Eintrag in die
    Begriffsliste erkannt wird.
    """
    return {
        "entities": [
            {
                "entity_type": definition.entity_type,
                "label": definition.label,
                "description": definition.description,
                "default_policy": definition.policy.value,
                "source": (
                    "custom_terms"
                    if definition.entity_type in ents.NUR_BEGRIFFSLISTE
                    else "recognizer"
                ),
            }
            for definition in ents.CATALOG.values()
        ]
    }
