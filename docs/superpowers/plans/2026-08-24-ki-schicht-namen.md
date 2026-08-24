# KI-Schicht für Personennamen — Umsetzungsplan

> **Für agentische Bearbeiter:** ERFORDERLICHER SUB-SKILL:
> `superpowers:subagent-driven-development` (empfohlen) oder
> `superpowers:executing-plans`. Die Schritte tragen Checkboxen (`- [ ]`).

**Ziel:** Der Namenssplit verschwindet — `Sandra Zbinden` wird vollständig
erkannt statt nur als `Sandra`, indem ein Schweizer KI-Dienst die Grenzen
zweifelhafter Personennamen korrigiert.

**Architektur:** Eine neue Schicht in `backend/ki/`, eingehängt in
`pipeline.detect_in_segments` zwischen lokaler Erkennung und
`propagate_findings`. Sie schickt ausschliesslich **Fenster von ±10 Wörtern**
um bereits gefundene `PERSON`-Stellen an Infomaniak AI, darf Fundgrenzen nur
**erweitern**, und fällt bei jedem Fehler auf das lokale Ergebnis zurück.

**Tech-Stack:** Python 3.13/3.14, httpx (bereits Abhängigkeit), Infomaniak AI
Tools (OpenAI-kompatibel), pytest, React/TypeScript + vitest.

**Spec:** `docs/superpowers/specs/2026-08-24-ki-schicht-namen-design.md`

## Globale Vorgaben

- **Nur erweitern, nie verwerfen.** Ein Vorschlag, der eine Fundgrenze
  verkleinert oder einen Fund entfernt, wird verworfen und protokolliert.
- **Nur `PERSON`**, und nur Funde aus dem spaCy-Erkenner
  (`recognition_metadata[RECOGNIZER_NAME_KEY] == "SpacyRecognizer"` ist auf
  Finding-Ebene nicht mehr verfügbar — stattdessen: alle `PERSON`-Funde, die
  nicht `propagated` sind).
- **`response_format` ist `json_schema`**, niemals `json_object` (ergibt bei
  Infomaniak HTTP 400, gemessen 2026-08-24).
- **HTTP 200 mit leerem `content` ist ein Fehler**, kein Ergebnis.
- **Zeitüberschreitung hart setzen**, nie den httpx-Vorgabewert.
- **Kein stiller Rückfall auf einen anderen Anbieter.** Fehlt der Schlüssel,
  meldet sich die KI-Prüfung sichtbar ab.
- **`temperature: 0`**, Modell `mistralai/Ministral-3-14B-Instruct-2512`.
- Vorgabe des Vertragsfelds `ki_pruefung`: **`false`** auf API-Ebene.
- UI-Texte, Kommentare und Commit-Messages auf **Deutsch**.
- Nach jeder Aufgabe: `.venv/Scripts/python -m pytest backend/tests -q` und
  `.venv/Scripts/python -m ruff check` müssen sauber sein.

---

## Dateien

| Datei | Verantwortung |
|---|---|
| `backend/ki/__init__.py` | Paket |
| `backend/ki/infomaniak.py` | HTTP: Aufruf, Fehlerbilder, Schlüsselprüfung |
| `backend/ki/namen.py` | Fachlogik: Fenster bauen, Antwort anwenden |
| `backend/pipeline.py` | Einhängepunkt (Änderung) |
| `backend/routers/documents.py` | Vertragsfeld `ki_pruefung` (Änderung) |
| `backend/config.py` | Modell-Vorgabe (Änderung) |
| `frontend/src/panels/Pseudonymisieren.tsx` | Ankreuzfeld, Warnhinweis (Änderung) |
| `frontend/src/core/api.ts` | Feld durchreichen (Änderung) |
| `backend/tests/test_ki_infomaniak.py` | neu |
| `backend/tests/test_ki_namen.py` | neu |
| `backend/tests/test_ki_pipeline.py` | neu |

---

### Aufgabe 1: Der HTTP-Baustein

**Dateien:**
- Anlegen: `backend/ki/__init__.py`, `backend/ki/infomaniak.py`
- Test: `backend/tests/test_ki_infomaniak.py`

**Schnittstellen:**
- Liefert: `ist_verfuegbar(einstellungen) -> bool`,
  `KiFehler`, `KiNichtKonfiguriert`,
  `InfomaniakKi(einstellungen, client=None).chat_json(nachrichten, schema, max_tokens) -> dict`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
# backend/tests/test_ki_infomaniak.py
"""Tests des HTTP-Bausteins zur Infomaniak-KI.

Der Client wird hereingereicht, nie im Baustein erzeugt — nur so ist jeder
Fehlerfall ohne Netzwerk pruefbar. Genau diese Faelle sind hier die Arbeit:
Ein Dienst, der bei Stoerung stillschweigend nichts liefert, waere fuer ein
Werkzeug, das Personendaten entfernt, das gefaehrlichste Verhalten.
"""

from __future__ import annotations

import pytest

from backend.ki.infomaniak import (
    InfomaniakKi,
    KiFehler,
    KiNichtKonfiguriert,
    ist_verfuegbar,
)


class Einstellungen:
    """Minimaler Ersatz fuer backend.config.Settings."""

    def __init__(self, key="k", produkt="110819"):
        self.infomaniak_ai_key = key
        self.infomaniak_ai_product_id = produkt
        self.infomaniak_ai_base = "https://api.infomaniak.com"
        self.infomaniak_ai_modell = "mistralai/Ministral-3-14B-Instruct-2512"


class Antwort:
    def __init__(self, status, daten=None):
        self.status_code = status
        self._daten = daten or {}
        self.text = str(daten)

    def json(self):
        return self._daten


class Client:
    """Zeichnet den Aufruf auf und liefert eine vorbereitete Antwort."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = []

    def post(self, url, **kw):
        self.aufrufe.append((url, kw))
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return self.antwort


def _antwort_mit(inhalt: str) -> Antwort:
    return Antwort(200, {"choices": [{"message": {"content": inhalt}}]})


def test_ohne_schluessel_nicht_verfuegbar():
    assert ist_verfuegbar(Einstellungen(key="")) is False
    assert ist_verfuegbar(Einstellungen(produkt="")) is False
    assert ist_verfuegbar(Einstellungen()) is True


def test_ohne_schluessel_klare_meldung():
    ki = InfomaniakKi(Einstellungen(key=""), client=Client(_antwort_mit("{}")))
    with pytest.raises(KiNichtKonfiguriert):
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})


def test_produkt_id_steht_im_pfad():
    client = Client(_antwort_mit('{"a": 1}'))
    ki = InfomaniakKi(Einstellungen(), client=client)
    ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    url, _ = client.aufrufe[0]
    assert url == (
        "https://api.infomaniak.com/2/ai/110819/openai/v1/chat/completions"
    )


def test_schema_statt_json_object():
    """`json_object` ergibt bei Infomaniak HTTP 400 — gemessen am 2026-08-24."""
    client = Client(_antwort_mit('{"a": 1}'))
    ki = InfomaniakKi(Einstellungen(), client=client)
    ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    _, kw = client.aufrufe[0]
    formatierung = kw["json"]["response_format"]
    assert formatierung["type"] == "json_schema"
    assert kw["json"]["temperature"] == 0


def test_leerer_inhalt_ist_ein_fehler():
    """HTTP 200 heisst nicht Erfolg.

    Vier Modelle des Katalogs liefern `content: null`, wenn das Token-Budget
    knapp ist. Wer nur `content` liest, sieht eine leere Antwort und keinen
    Fehler — und ein stillschweigend verlorener Befund ist genau das, was
    dieses Werkzeug nicht produzieren darf.
    """
    ki = InfomaniakKi(Einstellungen(), client=Client(_antwort_mit("")))
    with pytest.raises(KiFehler):
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})


def test_fehlerstatus_wird_zu_kifehler():
    ki = InfomaniakKi(Einstellungen(), client=Client(Antwort(429, {})))
    with pytest.raises(KiFehler):
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})


def test_ungueltiges_json_wird_zu_kifehler():
    ki = InfomaniakKi(Einstellungen(), client=Client(_antwort_mit("kein json")))
    with pytest.raises(KiFehler):
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})


def test_netzwerkfehler_wird_zu_kifehler():
    ki = InfomaniakKi(Einstellungen(), client=Client(OSError("weg")))
    with pytest.raises(KiFehler):
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_infomaniak.py -q`
Erwartet: FAIL mit `ModuleNotFoundError: No module named 'backend.ki'`

- [ ] **Schritt 3: Minimale Umsetzung**

```python
# backend/ki/__init__.py
"""KI-Schicht (Phase 3, Variante C — Infomaniak AI Tools).

Getrennt in zwei Bausteine: ``infomaniak`` redet HTTP, ``namen`` redet Text.
Der Grund steht in der Spec unter 3.1 — ein Wechsel der Ausbaustufe (welcher
Textumfang das Haus verlaesst) beruehrt dann nur ``namen``.
"""
```

```python
# backend/ki/infomaniak.py
"""Schmale Huelle um den Chat-Aufruf der Infomaniak AI Tools.

Vier Dinge sind am echten Dienst gemessen (2026-08-24) und nicht der Doku
entnommen — jedes einzelne laesst Standardcode scheitern:

1. ``response_format: {"type": "json_object"}`` ergibt **HTTP 400**. Erlaubt
   ist ausschliesslich ``json_schema``.
2. **HTTP 200 heisst nicht Erfolg.** Vier Modelle liefern ``content: null``,
   wenn das Token-Budget knapp ist; wer nur ``content`` liest, sieht eine
   leere Antwort und keinen Fehler.
3. Der Katalogeintrag ``info_status`` luegt — nie danach filtern.
4. Die **Produkt-Id steht im Pfad**, nicht im Kopf. Der Bearer-Token
   authentisiert nur.

Der HTTP-Client wird **hereingereicht**. Ohne das waere kein Fehlerfall ohne
Netzwerk pruefbar, und genau die Fehlerfaelle sind hier die eigentliche
Arbeit.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger("pti.ki.infomaniak")

__all__ = [
    "InfomaniakKi",
    "KiFehler",
    "KiNichtKonfiguriert",
    "ist_verfuegbar",
]

#: Pfadvorlage. ``{produkt}`` wird ersetzt. Der ``/1``-Pfad kennt Apertus
#: nicht (HTTP 422) — deshalb ``/2``.
CHAT_PFAD = "/2/ai/{produkt}/openai/v1/chat/completions"

#: Hart gesetzt, nie der httpx-Vorgabewert. Sonst wird aus „weiterlaufen bei
#: Ausfall" ein „haengen bei Ausfall" — derselbe Stolperstein wie beim
#: SSO-Gate (siehe CLAUDE.md).
ZEITGRENZE_SEKUNDEN = 20.0


class HttpClient(Protocol):
    def post(self, url: str, **kw: Any) -> Any: ...


class KiFehler(RuntimeError):
    """Der Dienst hat nicht brauchbar geantwortet."""


class KiNichtKonfiguriert(KiFehler):
    """Schluessel oder Produkt-Id fehlen — ein Zustand, kein Ausfall."""


def ist_verfuegbar(einstellungen) -> bool:
    """Ob Schluessel **und** Produkt-Id gesetzt sind.

    Beides wird gebraucht: Die Produkt-Id steht im Pfad der Anfrage.
    """
    return bool(
        einstellungen.infomaniak_ai_key and einstellungen.infomaniak_ai_product_id
    )


class InfomaniakKi:
    """Ein Aufruf: Chat mit erzwungenem JSON-Schema."""

    def __init__(self, einstellungen, client: HttpClient | None = None) -> None:
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

        Raises
        ------
        KiNichtKonfiguriert
            Schluessel oder Produkt-Id fehlen.
        KiFehler
            Netzwerkfehler, Fehlerstatus, leerer Inhalt oder ungueltiges JSON.
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
            "temperature": 0,
            "max_tokens": max_tokens,
            # `json_object` ergaebe HTTP 400 — siehe Modulkopf.
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
            raise KiFehler(f"Der KI-Dienst antwortete mit {antwort.status_code}.")

        try:
            inhalt = antwort.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as fehler:
            raise KiFehler("Die Antwort hatte nicht die erwartete Form.") from fehler

        if not inhalt or not inhalt.strip():
            # HTTP 200 heisst nicht Erfolg — siehe Modulkopf, Punkt 2.
            raise KiFehler("Der KI-Dienst lieferte eine leere Antwort.")

        try:
            return json.loads(inhalt)
        except json.JSONDecodeError as fehler:
            raise KiFehler("Die Antwort war kein gueltiges JSON.") from fehler
```

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_infomaniak.py -q`
Erwartet: 8 passed

- [ ] **Schritt 5: Committen**

```bash
git add backend/ki/ backend/tests/test_ki_infomaniak.py
git commit -m "feat(ki): HTTP-Baustein fuer die Infomaniak-KI"
```

---

### Aufgabe 2: Fenster bauen

**Dateien:**
- Anlegen: `backend/ki/namen.py`
- Test: `backend/tests/test_ki_namen.py`

**Schnittstellen:**
- Verbraucht: nichts aus Aufgabe 1
- Liefert: `Fenster` (dataclass: `nummer:int`, `finding_index:int`,
  `text:str`, `versatz:int`, `markiert:str`), `baue_fenster(segments, findings, wortradius=10) -> list[Fenster]`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
# backend/tests/test_ki_namen.py
"""Tests der Fachlogik der KI-Schicht.

Zwei Dinge werden hier gepruefti: dass die Fenster **nicht** mehr Text
mitnehmen als vereinbart (Spec 2.1 — nur das verlaesst das Haus), und dass
die Antwort Fundgrenzen nur erweitert (Spec 2.3).
"""

from __future__ import annotations

from backend.adapters import Segment
from backend.ki.namen import baue_fenster
from backend.pipeline import Finding


def _segment(text: str) -> Segment:
    return Segment(id="text", text=text, location=None)


def test_fenster_umfasst_den_fund_und_kontext():
    text = "Die Auskunft erteilte Frau Sandra Zbinden am Montag im Amt."
    start = text.index("Sandra")
    f = Finding("text", "PERSON", start, start + len("Sandra"), 0.85, "Sandra")
    (fenster,) = baue_fenster([_segment(text)], [f], wortradius=3)
    assert "Sandra" in fenster.text
    assert "Zbinden" in fenster.text
    assert fenster.markiert == "Sandra"


def test_fenster_bleibt_im_segment():
    """Ueber die Segmentgrenze hinaus geht nichts — auch nicht ein Wort."""
    text = "Sandra Zbinden"
    f = Finding("text", "PERSON", 0, 6, 0.85, "Sandra")
    (fenster,) = baue_fenster([_segment(text)], [f], wortradius=50)
    assert fenster.text == text


def test_wortradius_begrenzt_den_umfang():
    """Der Kern von Spec 2.1: Es geht wirklich nur ein Fenster hinaus."""
    text = " ".join(["wort"] * 40) + " Sandra " + " ".join(["wort"] * 40)
    start = text.index("Sandra")
    f = Finding("text", "PERSON", start, start + 6, 0.85, "Sandra")
    (fenster,) = baue_fenster([_segment(text)], [f], wortradius=5)
    assert len(fenster.text.split()) <= 11
    assert len(fenster.text) < len(text) / 3


def test_nur_person_und_nicht_uebertragene():
    """Uebertragene Funde stammen aus einem anderen, schon geprueften Fund."""
    text = "Sandra und die Kantonspolizei Bern"
    findings = [
        Finding("text", "PERSON", 0, 6, 0.85, "Sandra"),
        Finding("text", "ORGANIZATION", 15, 29, 0.85, "Kantonspolizei"),
        Finding("text", "PERSON", 30, 34, 0.85, "Bern", propagated=True),
    ]
    fenster = baue_fenster([_segment(text)], findings, wortradius=3)
    assert len(fenster) == 1
    assert fenster[0].finding_index == 0


def test_nummern_sind_fortlaufend_und_stabil():
    text = "Sandra kam. Beat ging."
    findings = [
        Finding("text", "PERSON", 0, 6, 0.85, "Sandra"),
        Finding("text", "PERSON", 12, 16, 0.85, "Beat"),
    ]
    fenster = baue_fenster([_segment(text)], findings, wortradius=2)
    assert [f.nummer for f in fenster] == [0, 1]
    assert [f.finding_index for f in fenster] == [0, 1]
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_namen.py -q`
Erwartet: FAIL mit `ImportError: cannot import name 'baue_fenster'`

- [ ] **Schritt 3: Minimale Umsetzung**

```python
# backend/ki/namen.py
"""Fachlogik der KI-Schicht: Fenster bauen, Antwort anwenden.

Dieser Baustein bestimmt, **wieviel Text das Haus verlaesst**. Er ist deshalb
von ``infomaniak.py`` getrennt: Ein Wechsel der Ausbaustufe (Spec 8 — Fenster
gegen ganze Segmente gegen ganzes Dokument) beruehrt nur diese Datei.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Sequence

from backend.adapters import Segment

logger = logging.getLogger("pti.ki.namen")

__all__ = ["Fenster", "baue_fenster", "WORTRADIUS"]

#: Woerter links und rechts des Fundes. Spec 2.1.
WORTRADIUS = 10

_WORTGRENZE = re.compile(r"\S+")


@dataclass(frozen=True)
class Fenster:
    """Ein Textausschnitt um genau einen Fundkandidaten."""

    #: Fortlaufend, beginnt bei 0. Die Antwort adressiert darueber.
    nummer: int
    #: Index in der uebergebenen Fundliste — der Rueckweg zum ``Finding``.
    finding_index: int
    #: Der Ausschnitt. **Nur das verlaesst das Haus.**
    text: str
    #: Startversatz von ``text`` im Segmenttext.
    versatz: int
    #: Der markierte Fundtext selbst.
    markiert: str


def _wortgrenzen(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _WORTGRENZE.finditer(text)]


def baue_fenster(
    segments: Sequence[Segment],
    findings: Sequence,
    wortradius: int = WORTRADIUS,
) -> list[Fenster]:
    """Baut je Fundkandidat ein Fenster.

    Beruecksichtigt werden ausschliesslich Funde vom Typ ``PERSON``, die
    **nicht** uebertragen sind: Ein uebertragener Fund stammt aus einem
    anderen, bereits geprueften — ihn erneut zu pruefen kostet nur.

    Das Fenster endet **immer** an der Segmentgrenze. Ueber sie hinaus gibt es
    keinen Kontext, der uns gehoerte.
    """
    nach_id = {s.id: s for s in segments}
    fenster: list[Fenster] = []
    nummer = 0

    for index, fund in enumerate(findings):
        if fund.entity_type != "PERSON" or getattr(fund, "propagated", False):
            continue
        segment = nach_id.get(fund.segment_id)
        if segment is None:
            continue

        worte = _wortgrenzen(segment.text)
        # Erstes Wort, das den Fund beruehrt, und letztes ebenso.
        beruehrt = [i for i, (a, b) in enumerate(worte) if a < fund.end and b > fund.start]
        if not beruehrt:
            continue
        von = max(0, beruehrt[0] - wortradius)
        bis = min(len(worte) - 1, beruehrt[-1] + wortradius)
        versatz = worte[von][0]
        ende = worte[bis][1]

        fenster.append(
            Fenster(
                nummer=nummer,
                finding_index=index,
                text=segment.text[versatz:ende],
                versatz=versatz,
                markiert=segment.text[fund.start : fund.end],
            )
        )
        nummer += 1

    return fenster
```

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_namen.py -q`
Erwartet: 5 passed

- [ ] **Schritt 5: Committen**

```bash
git add backend/ki/namen.py backend/tests/test_ki_namen.py
git commit -m "feat(ki): Fenster um Fundkandidaten bauen"
```

---

### Aufgabe 3: Die Antwort anwenden — nur erweitern

**Dateien:**
- Ändern: `backend/ki/namen.py`
- Test: `backend/tests/test_ki_namen.py` (ergänzen)

**Schnittstellen:**
- Verbraucht: `Fenster` aus Aufgabe 2
- Liefert: `SCHEMA` (dict), `baue_nachrichten(fenster) -> list[dict]`,
  `wende_an(findings, fenster, antwort) -> tuple[list, int]`
  (neue Fundliste, Anzahl verworfener Vorschläge)

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
# ergaenzt backend/tests/test_ki_namen.py
from backend.ki.namen import wende_an


def _fund(text, wort, propagated=False):
    start = text.index(wort)
    return Finding("text", "PERSON", start, start + len(wort), 0.85, wort, propagated)


def test_grenze_wird_erweitert():
    text = "Es schrieb Sandra Zbinden am Montag."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings, fenster, {"funde": [{"nummer": 0, "name": "Sandra Zbinden",
                                       "ist_person": True}]}
    )
    assert neu[0].text == "Sandra Zbinden"
    assert text[neu[0].start : neu[0].end] == "Sandra Zbinden"
    assert verworfen == 0


def test_verkleinern_wird_verworfen():
    """Spec 2.3: nur erweitern.

    Ein verkleinerter Fund liesse einen Namensteil im Dokument stehen — das
    gefaehrlichste Fehlerbild dieses Werkzeugs.
    """
    text = "Es schrieb Sandra Zbinden am Montag."
    findings = [_fund(text, "Sandra Zbinden")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings, fenster, {"funde": [{"nummer": 0, "name": "Sandra",
                                       "ist_person": True}]}
    )
    assert neu[0].text == "Sandra Zbinden"
    assert verworfen == 1


def test_ist_person_false_wird_ignoriert():
    """Verwerfen darf die KI nicht — aber es wird gezaehlt."""
    text = "Es schrieb Sandra Zbinden am Montag."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings, fenster, {"funde": [{"nummer": 0, "name": "Sandra",
                                       "ist_person": False}]}
    )
    assert len(neu) == 1
    assert neu[0].text == "Sandra"
    assert verworfen == 1


def test_unbekannte_nummer_wird_verworfen():
    text = "Es schrieb Sandra Zbinden."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings, fenster, {"funde": [{"nummer": 99, "name": "X",
                                       "ist_person": True}]}
    )
    assert neu[0].text == "Sandra"
    assert verworfen == 1


def test_name_der_im_fenster_nicht_vorkommt_wird_verworfen():
    """Das Modell darf nichts erfinden."""
    text = "Es schrieb Sandra Zbinden."
    findings = [_fund(text, "Sandra")]
    fenster = baue_fenster([_segment(text)], findings)
    neu, verworfen = wende_an(
        findings, fenster, {"funde": [{"nummer": 0, "name": "Ganz Anders",
                                       "ist_person": True}]}
    )
    assert neu[0].text == "Sandra"
    assert verworfen == 1


def test_andere_funde_bleiben_unberuehrt():
    text = "Sandra Zbinden und die Kantonspolizei."
    findings = [
        _fund(text, "Sandra"),
        Finding("text", "ORGANIZATION", 23, 37, 0.85, "Kantonspolizei"),
    ]
    fenster = baue_fenster([_segment(text)], findings)
    neu, _ = wende_an(
        findings, fenster, {"funde": [{"nummer": 0, "name": "Sandra Zbinden",
                                       "ist_person": True}]}
    )
    assert neu[1].entity_type == "ORGANIZATION"
    assert neu[1].text == "Kantonspolizei"
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_namen.py -q`
Erwartet: FAIL mit `ImportError: cannot import name 'wende_an'`

- [ ] **Schritt 3: Minimale Umsetzung**

```python
# ergaenzt backend/ki/namen.py
from dataclasses import replace

#: Antwortschema. ``json_object`` ergaebe HTTP 400 — nur ``json_schema``.
SCHEMA = {
    "type": "object",
    "properties": {
        "funde": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nummer": {"type": "integer"},
                    "name": {"type": "string"},
                    "ist_person": {"type": "boolean"},
                },
                "required": ["nummer", "name", "ist_person"],
            },
        }
    },
    "required": ["funde"],
}

_ANWEISUNG = (
    "Du pruefst markierte Textstellen in deutschsprachigen Behoerdendokumenten. "
    "Zu jedem nummerierten Ausschnitt ist eine Stelle mit >>> <<< markiert. "
    "Sage fuer jede Nummer: ist die Markierung Teil eines Personennamens, und "
    "wie lautet der VOLLSTAENDIGE Name genau so, wie er im Ausschnitt steht? "
    "Gib den Namen buchstabengetreu aus dem Ausschnitt wieder, erfinde nichts "
    "und ergaenze nichts. Titel und Anreden (Herr, Frau, Dr.) gehoeren nicht "
    "zum Namen."
)


def baue_nachrichten(fenster: Sequence[Fenster]) -> list[dict]:
    """Baut die Chat-Nachrichten. Nur die Fenstertexte gehen hinaus."""
    zeilen = []
    for f in fenster:
        markiert = f.text.replace(f.markiert, f">>>{f.markiert}<<<", 1)
        zeilen.append(f"{f.nummer}: {markiert}")
    return [
        {"role": "system", "content": _ANWEISUNG},
        {"role": "user", "content": "\n".join(zeilen)},
    ]


def wende_an(
    findings: Sequence,
    fenster: Sequence[Fenster],
    antwort: dict,
) -> tuple[list, int]:
    """Wendet die Vorschlaege an — **nur erweiternd**.

    Ein Vorschlag wird verworfen, wenn er eine Grenze verkleinert, den Fund
    entfernen will (``ist_person: false``), eine unbekannte Nummer nennt oder
    einen Namen liefert, der im Fenster gar nicht vorkommt.

    Returns
    -------
    (neue_findings, anzahl_verworfen)
        Die Zahl ist kein Schmuck: Aus ihr laesst sich spaeter ablesen, ob
        sich die naechste Ausbaustufe lohnt (Spec 8).
    """
    nach_nummer = {f.nummer: f for f in fenster}
    neu = list(findings)
    verworfen = 0

    for eintrag in antwort.get("funde", []):
        nummer = eintrag.get("nummer")
        name = (eintrag.get("name") or "").strip()
        ist_person = eintrag.get("ist_person")

        f = nach_nummer.get(nummer)
        if f is None or not name:
            verworfen += 1
            continue
        if ist_person is False:
            # Verwerfen ist nicht erlaubt (Spec 2.3) — aber es wird gezaehlt.
            verworfen += 1
            continue

        stelle = f.text.find(name)
        if stelle < 0:
            # Das Modell hat etwas geliefert, das im Ausschnitt nicht steht.
            verworfen += 1
            continue

        alt = neu[f.finding_index]
        start = f.versatz + stelle
        ende = start + len(name)
        if start > alt.start or ende < alt.end or (start == alt.start and ende == alt.end):
            verworfen += 1
            continue

        neu[f.finding_index] = replace(alt, start=start, end=ende, text=name)

    if verworfen:
        logger.info("KI-Vorschlaege verworfen: %d", verworfen)
    return neu, verworfen
```

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_namen.py -q`
Erwartet: 11 passed

- [ ] **Schritt 5: Committen**

```bash
git add backend/ki/namen.py backend/tests/test_ki_namen.py
git commit -m "feat(ki): Antwort anwenden — Fundgrenzen nur erweitern"
```

---

### Aufgabe 4: Einhängen in die Pipeline

**Dateien:**
- Ändern: `backend/pipeline.py` (`detect_in_segments`)
- Test: `backend/tests/test_ki_pipeline.py`

**Schnittstellen:**
- Verbraucht: `baue_fenster`, `baue_nachrichten`, `wende_an`, `SCHEMA`,
  `InfomaniakKi`, `KiFehler`, `ist_verfuegbar`
- Liefert: `DetectionResult.ki_geprueft: bool`, Parameter
  `ki_pruefung: bool = False` und `ki=None` an `detect_in_segments`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
# backend/tests/test_ki_pipeline.py
"""Die KI-Schicht am Einhaengepunkt.

Der wichtigste Test ist der letzte: Faellt der Dienst aus, liefert der Lauf
weiterhin das lokale Ergebnis. Ein Werkzeug, das bei einer Stoerung beim
Dienstleister stillsteht, waere ein Rueckschritt gegenueber dem Zustand ohne
KI-Schicht (Spec 2.2).
"""

from __future__ import annotations

from backend import pipeline
from backend.adapters import Segment
from backend.ki.infomaniak import KiFehler


class KiDoppel:
    """Liefert eine vorbereitete Antwort oder wirft."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = 0

    def chat_json(self, nachrichten, schema, max_tokens=2000):
        self.aufrufe += 1
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return self.antwort


TEXT = "Die Auskunft erteilte Frau Sandra Zbinden am Montag."


def _segmente():
    return [Segment(id="text", text=TEXT, location=None)]


def test_ohne_anforderung_wird_nicht_gefragt():
    ki = KiDoppel({"funde": []})
    ergebnis = pipeline.detect_in_segments(_segmente(), ki=ki)
    assert ki.aufrufe == 0
    assert ergebnis.ki_geprueft is False


def test_mit_anforderung_wird_die_grenze_korrigiert():
    ki = KiDoppel(
        {"funde": [{"nummer": 0, "name": "Sandra Zbinden", "ist_person": True}]}
    )
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 1
    assert ergebnis.ki_geprueft is True
    personen = [f.text for f in ergebnis.findings if f.entity_type == "PERSON"]
    assert "Sandra Zbinden" in personen


def test_ausfall_liefert_das_lokale_ergebnis():
    ki = KiDoppel(KiFehler("Dienst weg"))
    ergebnis = pipeline.detect_in_segments(_segmente(), ki_pruefung=True, ki=ki)
    assert ergebnis.ki_geprueft is False
    assert ergebnis.findings, "die lokalen Funde muessen erhalten bleiben"


def test_ohne_fundkandidaten_kein_aufruf():
    """Nichts gefunden, nichts zu pruefen — und nichts geht hinaus."""
    ki = KiDoppel({"funde": []})
    leer = [Segment(id="text", text="Zahlen: 1 2 3.", location=None)]
    ergebnis = pipeline.detect_in_segments(leer, ki_pruefung=True, ki=ki)
    assert ki.aufrufe == 0
    assert ergebnis.ki_geprueft is False
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_pipeline.py -q`
Erwartet: FAIL mit `TypeError: detect_in_segments() got an unexpected keyword argument 'ki'`

- [ ] **Schritt 3: Minimale Umsetzung**

In `backend/pipeline.py`: `DetectionResult` um ein Feld ergänzen —

```python
@dataclass
class DetectionResult:
    """Ergebnis eines reinen Erkennungslaufs, ohne Ersetzung."""

    segments: list[Segment]
    findings: list[Finding] = field(default_factory=list)
    #: ``True``, wenn die KI-Schicht tatsaechlich gelaufen ist. ``False``
    #: heisst: nicht angefordert, nicht konfiguriert, nichts zu pruefen — oder
    #: der Dienst hat nicht geantwortet. Der Aufrufer muss wissen, was er in
    #: der Hand haelt.
    ki_geprueft: bool = False
```

In `detect_in_segments` die Signatur erweitern und den Block **vor**
`propagate_findings` einsetzen:

```python
def detect_in_segments(
    segments: Sequence[Segment],
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
    propagate: bool = True,
    ki_pruefung: bool = False,
    ki=None,
) -> DetectionResult:
```

```python
    # ── KI-Schicht (Phase 3, Variante C) ──────────────────────────────────
    # Sitzt bewusst VOR propagate_findings: Korrigiert die KI `Sandra` zu
    # `Sandra Zbinden`, traegt die Uebertragung anschliessend den
    # VOLLSTAENDIGEN Namen durchs Dokument — auch an Stellen, die spaCy
    # uebersehen hat. Umgekehrt waere die Wirkung deutlich kleiner.
    if ki_pruefung:
        result.findings, result.ki_geprueft = _ki_pruefen(
            result.segments, result.findings, ki
        )

    if propagate:
        result.findings = propagate_findings(result.segments, result.findings)
    return result


def _ki_pruefen(segments, findings, ki) -> tuple[list[Finding], bool]:
    """Laesst die KI die Namensgrenzen pruefen. Faellt nie hart aus.

    Jeder Fehler — nicht konfiguriert, Zeitgrenze, Fehlerstatus, leere oder
    ungueltige Antwort — fuehrt zum lokalen Ergebnis und ``False``. Der
    Aufrufer zeigt das an; still schlechter zu erkennen waere die
    gefaehrlichere Variante (Spec 2.2).
    """
    from backend.config import get_settings
    from backend.ki import namen
    from backend.ki.infomaniak import InfomaniakKi, KiFehler, ist_verfuegbar

    einstellungen = get_settings()
    if ki is None:
        if not ist_verfuegbar(einstellungen):
            logger.info("KI-Pruefung angefordert, aber nicht konfiguriert.")
            return list(findings), False
        ki = InfomaniakKi(einstellungen)

    fenster = namen.baue_fenster(segments, findings)
    if not fenster:
        return list(findings), False

    try:
        antwort = ki.chat_json(namen.baue_nachrichten(fenster), namen.SCHEMA)
    except KiFehler as fehler:
        logger.warning("KI-Pruefung fehlgeschlagen: %s", fehler)
        return list(findings), False

    logger.info(
        "KI-Pruefung: %d Fenster, Modell %s",
        len(fenster),
        einstellungen.infomaniak_ai_modell,
    )
    neu, _verworfen = namen.wende_an(findings, fenster, antwort)
    return neu, True
```

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_ki_pipeline.py backend/tests -q`
Erwartet: alle grün (die bestehenden Tests rufen ohne `ki_pruefung` auf und
bleiben unberührt)

- [ ] **Schritt 5: Committen**

```bash
git add backend/pipeline.py backend/tests/test_ki_pipeline.py
git commit -m "feat(ki): KI-Pruefung in die Pipeline einhaengen"
```

---

### Aufgabe 5: Der Vertrag nach aussen

**Dateien:**
- Ändern: `backend/routers/documents.py`
- Test: `backend/tests/test_integration_api.py` (ergänzen)

**Schnittstellen:**
- Verbraucht: `ki_pruefung` aus Aufgabe 4
- Liefert: Feld `ki_pruefung` in `TextRequest` und als Formularfeld;
  `ki_geprueft` in allen drei Antworten

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
# ergaenzt backend/tests/test_integration_api.py
def test_ki_pruefung_ist_standardmaessig_aus(client: TestClient) -> None:
    """Vorgabe `false` auf API-Ebene.

    Der Maschinenpfad /api/ext/v1 haengt dieselben Router ein. Waere die
    Vorgabe `true`, begaennen Translator, PPM und HIKA ab dem Deploy
    stillschweigend, Dokumentinhalte hinauszuschicken.
    """
    r = client.post("/api/v1/documents:detect", json={"text": TEXT})
    assert r.status_code == 200
    assert r.json()["ki_geprueft"] is False


def test_ki_pruefung_ohne_schluessel_meldet_sich_ab(client: TestClient) -> None:
    """Angefordert, aber nicht konfiguriert: kein Fehler, nur `false`."""
    r = client.post(
        "/api/v1/documents:detect", json={"text": TEXT, "ki_pruefung": True}
    )
    assert r.status_code == 200
    assert r.json()["ki_geprueft"] is False
    assert r.json()["entities"], "die lokale Erkennung muss weiterlaufen"
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_integration_api.py -k ki_pruefung -q`
Erwartet: FAIL mit `KeyError: 'ki_geprueft'`

- [ ] **Schritt 3: Minimale Umsetzung**

In `TextRequest` ergänzen:

```python
    ki_pruefung: bool = Field(
        False,
        description=(
            "KI-gestuetzte Pruefung der Namensgrenzen ueber einen Schweizer "
            "Dienst. Vorgabe aus: Ein Maschinen-Aufrufer muss den "
            "Datenversand ausdruecklich anfordern."
        ),
    )
```

In `anonymize_file` als Formularfeld:

```python
    ki_pruefung: Annotated[bool, Form()] = False,
```

An allen drei Stellen `ki_pruefung=payload.ki_pruefung` (bzw. den
Formularwert) an `pipeline.detect_in_segments` durchreichen und
`"ki_geprueft": result.ki_geprueft` in die Antwort aufnehmen.

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests -q`
Erwartet: alle grün

- [ ] **Schritt 5: Committen**

```bash
git add backend/routers/documents.py backend/tests/test_integration_api.py
git commit -m "feat(api): ki_pruefung anfordern, ki_geprueft zurueckmelden"
```

---

### Aufgabe 6: Oberfläche

**Dateien:**
- Ändern: `frontend/src/core/api.ts`, `frontend/src/panels/Pseudonymisieren.tsx`
- Test: `frontend/src/panels/Pseudonymisieren.test.tsx` (ergänzen)

**Schnittstellen:**
- Verbraucht: Feld `ki_pruefung` aus Aufgabe 5
- Liefert: Ankreuzfeld, `Ergebnis.kiGeprueft: boolean`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```tsx
// ergaenzt frontend/src/panels/Pseudonymisieren.test.tsx
describe('KI-Pruefung', () => {
  it('ist vorangehakt und wird mitgeschickt', async () => {
    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');

    const feld = screen.getByRole('checkbox', { name: /KI-Prüfung/ });
    expect(feld).toBeChecked();

    await nutzer.type(screen.getByRole('textbox'), 'Es schrieb Andrea Beispiel.');
    await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

    await waitFor(() => expect(vi.mocked(anonymisiereText)).toHaveBeenCalled());
    expect(vi.mocked(anonymisiereText).mock.calls[0][4]).toBe(true);
  });

  it('warnt, wenn die KI-Pruefung nicht gelaufen ist', async () => {
    vi.mocked(anonymisiereText).mockResolvedValue({
      ...ERGEBNIS,
      kiGeprueft: false,
    } as never);
    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');
    await nutzer.type(screen.getByRole('textbox'), 'Text');
    await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

    expect(await screen.findByText(/nur lokal geprüft/)).toBeInTheDocument();
  });
});
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `cd frontend && npm test`
Erwartet: FAIL — `Unable to find role="checkbox"`

- [ ] **Schritt 3: Minimale Umsetzung**

`api.ts`: `Ergebnis` um `kiGeprueft: boolean` erweitern, in
`normalisiereAntwort` aus `roh.ki_geprueft === true` befüllen, und
`anonymisiereText`/`anonymisiereDatei` um einen Parameter `kiPruefung: boolean`
ergänzen, der als `ki_pruefung` mitgeht.

`Pseudonymisieren.tsx`: Zustand `const [kiPruefung, setKiPruefung] = useState(true)`,
ein Ankreuzfeld neben der Kategorienwahl mit der Beschriftung
„KI-Prüfung der Namensgrenzen (Schweizer Dienst)", `laufKiPruefung`
symmetrisch zu `laufModus` mitführen, und nach dem Lauf:

```tsx
{ergebnis && laufKiPruefung && !ergebnis.kiGeprueft && (
  <Hinweis ton="warnung">
    Die KI-Prüfung war nicht erreichbar — dieses Ergebnis wurde
    <strong> nur lokal geprüft</strong>. Prüfen Sie die Fundliste besonders
    sorgfältig auf unvollständig erkannte Namen.
  </Hinweis>
)}
```

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `cd frontend && npm test && npm run type-check && npm run build`
Erwartet: alle grün

- [ ] **Schritt 5: Committen**

```bash
git add frontend/src
git commit -m "feat(gui): Ankreuzfeld fuer die KI-Pruefung und Warnhinweis"
```

---

### Aufgabe 7: Modellwechsel und Messung

**Dateien:**
- Ändern: `backend/config.py`, `.env.example`, `golden-set/README.md`
- Test: `backend/tests/test_config_infomaniak.py` (ergänzen)

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
# ergaenzt backend/tests/test_config_infomaniak.py
def test_modell_ist_fuer_massenklassifikation_gewaehlt() -> None:
    """Apertus ist ein Chat-Modell und stand nur da, weil pti-rag es nutzt.

    Unsere Aufgabe ist Massenklassifikation: viele kleine Anfragen, kurze
    Antworten. Dafuer ist Ministral-3 das empfohlene Modell und eines von nur
    dreien mit echtem `ready`-Status.
    """
    from backend.config import Settings

    vorgabe = Settings.model_fields["infomaniak_ai_modell"].default
    assert vorgabe == "mistralai/Ministral-3-14B-Instruct-2512"
```

- [ ] **Schritt 2: Test laufen lassen, Fehlschlag bestätigen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_config_infomaniak.py -q`
Erwartet: FAIL — Vorgabe ist noch `swiss-ai/Apertus-v1.5-70B`

- [ ] **Schritt 3: Minimale Umsetzung**

In `backend/config.py` die Vorgabe ändern und den Grund als Kommentar
festhalten; `.env.example` gleichziehen.

- [ ] **Schritt 4: Tests laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests -q && .venv/Scripts/python -m ruff check`
Erwartet: alle grün

- [ ] **Schritt 5: Committen**

```bash
git add backend/config.py .env.example backend/tests/test_config_infomaniak.py
git commit -m "feat(config): Ministral-3 statt Apertus fuer die Namenspruefung"
```

---

### Aufgabe 8: Dokumentation und die öffentliche Aussage

**Dateien:**
- Ändern: `CLAUDE.md`, `API.md`, `golden-set/README.md`
- Vorschlagen (nicht setzen): Fussnote in `frontend/src/App.tsx`

- [ ] **Schritt 1: `API.md` um das Feld ergänzen**

`ki_pruefung` und `ki_geprueft` bei allen drei Endpunkten, mit dem Hinweis,
dass die Vorgabe `false` ist und warum.

- [ ] **Schritt 2: `CLAUDE.md` um einen Abschnitt ergänzen**

Nach dem Muster der bestehenden Abschnitte: was die Schicht tut, die vier
Entscheidungen in einem Satz je, und der Verweis auf die Spec für die
Ausbaustufen.

- [ ] **Schritt 3: `golden-set/README.md`**

Festhalten, dass der Standardlauf **ohne** KI misst und warum (Vergleichbarkeit
mit allen Zahlen seit 2026-08-18), und wie ein Lauf mit KI angefordert wird.

- [ ] **Schritt 4: Die Fussnote dem Auftraggeber vorlegen**

Die heutige Aussage „es werden keine Inhalte an Dritte übermittelt" wird
falsch, sobald jemand das Häkchen setzt. **Formulierung vorschlagen, nicht
setzen** — sie ist eine Aussage gegenüber Kunden. Erst nach Freigabe ändern
und das Dossier (`landing`, §9) gleichziehen.

- [ ] **Schritt 5: Committen**

```bash
git add API.md CLAUDE.md golden-set/README.md
git commit -m "docs: KI-Pruefung in API.md, CLAUDE.md und Golden-Set-README"
```

---

## Selbstdurchsicht

**Spec-Abdeckung:** 2.1 → Aufgabe 2 · 2.2 → Aufgabe 4 · 2.3 → Aufgabe 3 ·
2.4 → Aufgaben 5+6 · 3 → Aufgaben 1–4 · 4.1 → Aufgabe 2 · 4.2 → Aufgabe 3 ·
4.3 → Aufgabe 7 · 5 → Aufgaben 1+4 · 6 → Aufgabe 5 · 7 → Aufgaben 7+8 ·
8 → Aufgabe 8 (Verweis auf die Spec) · 9 → Aufgabe 8, Schritt 4.

**Offen und bewusst nicht im Plan:** Die Stückelungsgrenze bei sehr vielen
Fenstern (Spec 4.1). Sie wird am echten Dienst gemessen, sobald Aufgabe 1
steht — eine geratene Zahl wäre schlimmer als keine.
