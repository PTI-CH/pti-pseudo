"""Schmale Huelle um den Chat-Aufruf der Infomaniak AI Tools.

Vier Dinge sind am echten Dienst gemessen (2026-08-24) und nicht der Doku
entnommen — jedes einzelne laesst Standardcode scheitern:

1. ``response_format: {"type": "json_object"}`` ergibt **HTTP 400**. Erlaubt
   ist ausschliesslich ``{"type": "json_schema", ...}``. Wer strukturierte
   Ausgabe nach OpenAI-Vorbild baut, laeuft hier zuerst auf.
2. **HTTP 200 heisst nicht Erfolg.** Vier Modelle des Katalogs liefern
   ``content: null``, wenn das Token-Budget knapp ist; sie schreiben zuerst
   nach ``message.reasoning``. Wer nur ``content`` liest, sieht eine leere
   Antwort und keinen Fehler.
3. Der Katalogeintrag ``info_status`` luegt — dreizehn von sechzehn Modellen
   melden ``coming_soon``, obwohl sie antworten. Nie danach filtern.
4. Die **Produkt-Id steht im Pfad**, nicht im Kopf. Der Bearer-Token
   authentisiert nur; ohne Produkt-Id laesst sich der Dienst nicht einmal
   adressieren.

Der HTTP-Client wird **hereingereicht**. Ohne das waere kein Fehlerfall ohne
Netzwerk pruefbar — und genau die Fehlerfaelle sind hier die eigentliche
Arbeit, weil die Schicht bei jedem von ihnen sauber auf das lokale Ergebnis
zurueckfallen muss.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger("pti.ki.infomaniak")

__all__ = [
    "CHAT_PFAD",
    "InfomaniakKi",
    "KiFehler",
    "KiNichtKonfiguriert",
    "ZEITGRENZE_SEKUNDEN",
    "ist_verfuegbar",
]

#: Pfadvorlage; ``{produkt}`` wird ersetzt.
#:
#: Der ``/1``-Pfad nimmt nur eine kleine Modellauswahl und antwortet auf
#: andere mit HTTP 422 — deshalb ``/2``.
CHAT_PFAD = "/2/ai/{produkt}/openai/v1/chat/completions"

#: Hart gesetzt, nie der httpx-Vorgabewert (der keiner ist).
#:
#: Ohne eigene Grenze wird aus „weiterlaufen bei Ausfall" ein „haengen bei
#: Ausfall" — derselbe Stolperstein, der beim SSO-Gate in CLAUDE.md steht.
#:
#: **45 s** — reichlich bemessen, und das mit Absicht.
#:
#: Mit Apertus brauchte am 2026-08-24 keine Anfrage des Golden-Sets laenger
#: als **2.2 s** (ein bis sechs Fenster, neun von neun gelungen). Die Grenze
#: liegt also weit ueber dem Beobachteten.
#:
#: Der grosse Abstand ist die Lehre aus einem Irrweg: Mit einer Grenze von
#: 20 s sah ein ganz anderes Problem wie ein Zeitproblem aus. Das damals
#: eingesetzte Modell degenerierte bei manchen Eingaben in eine Schleife aus
#: Leerzeichen, und der Abbruch an der Zeitgrenze verdeckte, dass gar nicht
#: die Leitung langsam war. Eine knappe Grenze verwandelt fremde Fehler in
#: scheinbar eigene.
ZEITGRENZE_SEKUNDEN = 45.0


class HttpClient(Protocol):
    """Das Wenige, das dieser Baustein von einem HTTP-Client braucht."""

    def post(self, url: str, **kw: Any) -> Any: ...


# N818 verlangt die Endung `Error`. Hier bewusst nicht: Der Name ist identisch
# zu `pti-rag/backend/ki/infomaniak.py`, wo dieselbe Anbindung laeuft — zwei
# Schreibweisen fuer dieselbe Sache waeren beim Uebertragen von Erkenntnissen
# zwischen den Projekten die erste Fehlerquelle. Deutsche Bezeichner sind in
# diesem Baum ohnehin die Hausschreibweise (`wortarten.py`, `pipeline.py`).
class KiFehler(RuntimeError):  # noqa: N818
    """Der Dienst hat nicht brauchbar geantwortet.

    ``wiederholbar`` sagt, ob ein zweiter Versuch Aussicht auf Erfolg hat.
    Die Unterscheidung ist nicht kosmetisch: Ein haengender Aufruf kostet die
    volle Zeitgrenze, und ihn fuer ein HTTP 400 ein zweites Mal auf sich zu
    nehmen waere reine Wartezeit ohne jede Chance.
    """

    def __init__(self, *args, wiederholbar: bool = True) -> None:
        super().__init__(*args)
        self.wiederholbar = wiederholbar


class KiNichtKonfiguriert(KiFehler):  # noqa: N818
    """Schluessel oder Produkt-Id fehlen — ein Zustand, kein Ausfall."""

    def __init__(self, *args) -> None:
        # Ein fehlender Schluessel wird durch Wiederholen nicht gesetzt.
        super().__init__(*args, wiederholbar=False)


#: Fehlerstatus, bei denen ein zweiter Versuch sinnlos ist.
#:
#: 400 (falsches Schema), 401/403 (Schluessel), 404 (falsche Produkt-Id) sind
#: Fehler auf unserer Seite und bleiben beim zweiten Mal dieselben. 429 und
#: 5xx dagegen sind Zustaende des Dienstes und koennen vorbeigehen.
NICHT_WIEDERHOLBAR: frozenset[int] = frozenset({400, 401, 403, 404, 405, 422})


def ist_verfuegbar(einstellungen) -> bool:
    """Ob Schluessel **und** Produkt-Id gesetzt sind.

    Beides wird gebraucht: Die Produkt-Id steht im Pfad der Anfrage, der
    Token authentisiert nur.
    """
    return bool(
        einstellungen.infomaniak_ai_key and einstellungen.infomaniak_ai_product_id
    )


class InfomaniakKi:
    """Ein einziger Aufruf: Chat mit erzwungenem JSON-Schema."""

    def __init__(self, einstellungen, client: HttpClient | None = None) -> None:
        """``client`` wird hereingereicht; sonst entsteht einer beim Aufruf."""
        self.einstellungen = einstellungen
        self._client = client

    def _hole_client(self) -> HttpClient:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=ZEITGRENZE_SEKUNDEN)
        return self._client

    def _adresse(self) -> str:
        basis = self.einstellungen.infomaniak_ai_base.rstrip("/")
        return basis + CHAT_PFAD.format(
            produkt=self.einstellungen.infomaniak_ai_product_id
        )

    def chat_json(
        self,
        nachrichten: list[dict],
        schema: dict,
        max_tokens: int = 2000,
    ) -> dict:
        """Stellt eine Anfrage und gibt die geparste JSON-Antwort zurueck.

        Parameters
        ----------
        nachrichten
            Chat-Nachrichten im OpenAI-Format.
        schema
            JSON-Schema der erwarteten Antwort. Wird als ``json_schema``
            gesetzt — ``json_object`` ergaebe HTTP 400.
        max_tokens
            Grosszuegig lassen. Bei knappem Budget liefern manche Modelle
            HTTP 200 mit leerem Inhalt statt einer kuerzeren Antwort.

        Raises
        ------
        KiNichtKonfiguriert
            Schluessel oder Produkt-Id fehlen.
        KiFehler
            Netzwerkfehler, Fehlerstatus, unerwartete Antwortform, leerer
            Inhalt oder ungueltiges JSON.
        """
        if not ist_verfuegbar(self.einstellungen):
            raise KiNichtKonfiguriert(
                "Die KI-Pruefung ist nicht freigeschaltet. Setzen Sie "
                "INFOMANIAK_AI_KEY und INFOMANIAK_AI_PRODUCT_ID am Knoten "
                "und starten Sie ueber das Dashboard neu."
            )

        nutzlast = {
            "model": self.einstellungen.infomaniak_ai_modell,
            "messages": nachrichten,
            # Klassifikation braucht Wiederholbarkeit, keine Vielfalt.
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "antwort", "schema": schema},
            },
        }
        kopf = {
            "authorization": f"Bearer {self.einstellungen.infomaniak_ai_key}",
            "content-type": "application/json",
        }

        try:
            antwort = self._hole_client().post(
                self._adresse(),
                json=nutzlast,
                headers=kopf,
                timeout=ZEITGRENZE_SEKUNDEN,
            )
        except Exception as fehler:  # Netzwerk, DNS, Zeitgrenze
            raise KiFehler(f"Der KI-Dienst war nicht erreichbar: {fehler}") from fehler

        if antwort.status_code != 200:
            raise KiFehler(
                f"Der KI-Dienst antwortete mit {antwort.status_code}.",
                wiederholbar=antwort.status_code not in NICHT_WIEDERHOLBAR,
            )

        try:
            inhalt = antwort.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as fehler:
            raise KiFehler("Die Antwort hatte nicht die erwartete Form.") from fehler

        if not inhalt or not inhalt.strip():
            # Siehe Modulkopf, Punkt 2: HTTP 200 heisst nicht Erfolg.
            raise KiFehler("Der KI-Dienst lieferte eine leere Antwort.")

        try:
            return json.loads(inhalt)
        except json.JSONDecodeError as fehler:
            raise KiFehler("Die Antwort war kein gueltiges JSON.") from fehler
