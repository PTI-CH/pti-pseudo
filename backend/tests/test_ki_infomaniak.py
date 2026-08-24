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
    """Schluessel UND Produkt-Id werden gebraucht — die Id steht im Pfad."""
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
    assert url == "https://api.infomaniak.com/2/ai/110819/openai/v1/chat/completions"


def test_schema_statt_json_object():
    """``json_object`` ergibt bei Infomaniak HTTP 400 — gemessen 2026-08-24."""
    client = Client(_antwort_mit('{"a": 1}'))
    ki = InfomaniakKi(Einstellungen(), client=client)
    ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    _, kw = client.aufrufe[0]
    assert kw["json"]["response_format"]["type"] == "json_schema"
    assert kw["json"]["temperature"] == 0


def test_leerer_inhalt_ist_ein_fehler():
    """HTTP 200 heisst nicht Erfolg.

    Vier Modelle des Katalogs liefern ``content: null``, wenn das
    Token-Budget knapp ist. Wer nur ``content`` liest, sieht eine leere
    Antwort und keinen Fehler — und ein stillschweigend verlorener Befund ist
    genau das, was dieses Werkzeug nicht produzieren darf.
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
    """Auch ein roher OSError darf nicht nach draussen durchschlagen."""
    ki = InfomaniakKi(Einstellungen(), client=Client(OSError("weg")))
    with pytest.raises(KiFehler):
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})


def test_zeitgrenze_wird_gesetzt():
    """Nie der httpx-Vorgabewert.

    Sonst wird aus „weiterlaufen bei Ausfall" ein „haengen bei Ausfall" —
    derselbe Stolperstein wie beim SSO-Gate (CLAUDE.md).
    """
    client = Client(_antwort_mit('{"a": 1}'))
    ki = InfomaniakKi(Einstellungen(), client=client)
    ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    _, kw = client.aufrufe[0]
    assert kw["timeout"] > 0


# ── Wiederholbarkeit ───────────────────────────────────────────────────────


def test_zeitgrenze_ist_wiederholbar():
    """Ein haengender Aufruf ist der Fall, fuer den es die Wiederholung gibt."""
    ki = InfomaniakKi(Einstellungen(), client=Client(OSError("timed out")))
    with pytest.raises(KiFehler) as fehler:
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    assert fehler.value.wiederholbar is True


def test_leere_antwort_ist_wiederholbar():
    """Ein Modellaussetzer kann beim zweiten Versuch ausbleiben."""
    ki = InfomaniakKi(Einstellungen(), client=Client(_antwort_mit("")))
    with pytest.raises(KiFehler) as fehler:
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    assert fehler.value.wiederholbar is True


def test_serverfehler_und_ratenbegrenzung_sind_wiederholbar():
    for status in (429, 500, 502, 503):
        ki = InfomaniakKi(Einstellungen(), client=Client(Antwort(status, {})))
        with pytest.raises(KiFehler) as fehler:
            ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
        assert fehler.value.wiederholbar is True, f"HTTP {status}"


def test_klientfehler_ist_nicht_wiederholbar():
    """400/401/403 aendern sich beim zweiten Versuch nicht.

    Ein falsches Schema oder ein ungueltiger Schluessel bleiben falsch — eine
    Wiederholung kostete nur Zeit und einen zweiten vergeblichen Aufruf.
    """
    for status in (400, 401, 403, 404):
        ki = InfomaniakKi(Einstellungen(), client=Client(Antwort(status, {})))
        with pytest.raises(KiFehler) as fehler:
            ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
        assert fehler.value.wiederholbar is False, f"HTTP {status}"


def test_fehlende_konfiguration_ist_nicht_wiederholbar():
    ki = InfomaniakKi(Einstellungen(key=""), client=Client(_antwort_mit("{}")))
    with pytest.raises(KiNichtKonfiguriert) as fehler:
        ki.chat_json([{"role": "user", "content": "x"}], {"type": "object"})
    assert fehler.value.wiederholbar is False
