"""Golden-Set-Gate: prüft die Erkennungsqualität gegen die Zielmarken.

Fachkonzept §13.1 — lässt den Lauf rot werden, sobald eine Marke reisst.

Je Zielmarke genau ein Test, damit ein Fehlschlag sofort zeigt, *welche*
Marke gerissen ist — nicht nur, dass irgendetwas nicht stimmt:

* Struktur-PII (``CH_AHV``, ``CH_IBAN``, ``IBAN_CODE``, ``EMAIL_ADDRESS``,
  ``CH_PHONE``) muss 100 % Recall erreichen.
* ``PERSON`` muss mindestens 90 % Recall erreichen.
* Die Gesamt-Precision muss mindestens 90 % betragen.
* Kein Eintrag aus ``verboten`` darf jemals gefunden werden.

Reissen Zielmarken: die Marken selbst bleiben unangetastet, der Test bleibt
rot. Das ist Absicht — dieses Modul ist das Gate, nicht der Ort, an dem man
es sich bequem macht.

Das Golden-Set (``golden-set/synthetic/generator.py``) wird von einem
parallelen Arbeitsstrang gebaut. Solange die Datei fehlt, überspringt sich
dieses Modul vollständig — sobald sie existiert, laufen die Tests ohne
weiteres Zutun.

Die Erkennung lädt das deutsche spaCy-Modell (~7 s) — eine session-weite
Fixture lässt sie deshalb genau einmal über das komplette Golden-Set laufen;
jeder einzelne Test liest nur noch aus den bereits berechneten Kennzahlen.
"""

from __future__ import annotations

from typing import Callable, Iterable

import pytest

from scripts import golden_set_report as report

generator = report.lade_generator()

pytestmark = pytest.mark.skipif(
    generator is None,
    reason=(
        "golden-set/synthetic/generator.py fehlt noch — wird parallel "
        "gebaut. Sobald es existiert, laufen diese Tests automatisch."
    ),
)


@pytest.fixture(scope="session")
def auswertung() -> report.Gesamtauswertung:
    """Lässt die Erkennung genau einmal über das bewertete Golden-Set laufen.

    Dokumente mit ``nur_messen`` bleiben draussen. Sie vermessen eine
    Reichweitengrenze des Werkzeugs statt eine Zusicherung zu prüfen und
    liefern erwartungsgemäss schlechte Zahlen — im Bericht sichtbar, hier
    nicht sperrend. Die Begründung je Dokument steht am Dokument selbst.
    """
    dokumente = [d for d in generator.DOKUMENTE if not d.nur_messen]
    if not dokumente:
        pytest.skip("golden-set/synthetic/generator.py liefert keine Dokumente.")
    return report.werte_aus(dokumente)


def _liste(eintraege: Iterable, formatiere: Callable[[object], str]) -> str:
    """Baut eine mehrzeilige Detailliste für konkrete Fehlermeldungen."""
    eintraege = list(eintraege)
    if not eintraege:
        return "  (keine)"
    return "\n".join(f"  - {formatiere(e)}" for e in eintraege)


def _verfehlt_zeile(v) -> str:
    """Eine Zeile 'Dokument: Text — Grund' für eine verfehlte Erwartung."""
    grund = f" — {v.grund}" if v.grund else ""
    return f"[{v.dokument}] {v.text!r}{grund}"


def _fehlalarm_zeile(f) -> str:
    """Eine Zeile 'Dokument: Typ Text (score, Ampel)' für einen Fehlalarm."""
    grund = f" — {f.grund}" if f.grund else ""
    return (
        f"[{f.dokument}] {f.entity_type} {f.text!r} "
        f"(score={f.score:.2f}, ampel={f.confidence}){grund}"
    )


# ── Struktur-PII: 100 % Recall ──────────────────────────────────────────────


@pytest.mark.parametrize("entity_type", sorted(report.STRUKTUR_PII_TYPEN))
def test_struktur_pii_hat_hundert_prozent_recall(auswertung, entity_type):
    """AHV, IBAN (CH und generisch), E-Mail und Telefon dürfen nie fehlen."""
    kennzahlen = auswertung.kennzahlen(entity_type)
    if kennzahlen.erwartet == 0:
        pytest.skip(
            f"Kein Golden-Set-Eintrag für {entity_type} — Recall nicht messbar."
        )

    verfehlt = [v for v in auswertung.verfehlt if v.entity_type == entity_type]
    assert kennzahlen.recall == 1.0, (
        f"{entity_type}: Recall {kennzahlen.recall:.1%} "
        f"({kennzahlen.treffer}/{kennzahlen.erwartet}) statt geforderter "
        "100 %.\nVerfehlt:\n" + _liste(verfehlt, _verfehlt_zeile)
    )


# ── PERSON: mindestens 90 % Recall ──────────────────────────────────────────


def test_person_hat_mindestens_90_prozent_recall(auswertung):
    """Personennamen müssen mit mindestens 90 % Recall erkannt werden."""
    kennzahlen = auswertung.kennzahlen("PERSON")
    if kennzahlen.erwartet == 0:
        pytest.skip("Kein Golden-Set-Eintrag für PERSON — Recall nicht messbar.")

    verfehlt = [v for v in auswertung.verfehlt if v.entity_type == "PERSON"]
    assert kennzahlen.recall >= report.MIN_PERSON_RECALL, (
        f"PERSON: Recall {kennzahlen.recall:.1%} "
        f"({kennzahlen.treffer}/{kennzahlen.erwartet}) liegt unter der "
        f"geforderten Marke von {report.MIN_PERSON_RECALL:.0%}.\n"
        "Verfehlt:\n" + _liste(verfehlt, _verfehlt_zeile)
    )


# ── Precision gesamt: mindestens 90 % ───────────────────────────────────────


def test_precision_gesamt_ist_mindestens_90_prozent(auswertung):
    """Über alle bewerteten Typen hinweg muss die Precision mindestens 90 % sein."""
    gesamt = auswertung.kennzahlen()
    if gesamt.treffer + gesamt.fehlalarme == 0:
        pytest.skip("Keine Funde im Golden-Set — Precision nicht messbar.")

    assert gesamt.precision >= report.MIN_PRECISION_GESAMT, (
        f"Precision gesamt {gesamt.precision:.1%} "
        f"({gesamt.treffer} Treffer, {gesamt.fehlalarme} Fehlalarme) liegt "
        f"unter der geforderten Marke von {report.MIN_PRECISION_GESAMT:.0%}.\n"
        "Fehlalarme:\n" + _liste(auswertung.fehlalarme, _fehlalarm_zeile)
    )


# ── Verboten: darf nie gefunden werden ──────────────────────────────────────


def test_verbotene_funde_werden_nie_gefunden(auswertung):
    """Jeder Eintrag aus ``verboten`` darf in keinem Dokument auftauchen."""
    verbotene = auswertung.verbotene_funde
    assert not verbotene, (
        f"{len(verbotene)} verbotene Entität(en) wurden trotzdem gefunden:\n"
        + _liste(verbotene, _fehlalarm_zeile)
    )
