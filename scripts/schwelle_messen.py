#!/usr/bin/env python3
"""Misst, wie lange die Pseudonymisierung je Dateigrösse und Format dauert.

Wozu
----
``SYNC_THRESHOLD_BYTES`` (Vorgabe 2 MiB) entscheidet, ob eine Datei im
laufenden Request verarbeitet wird oder als Auftrag im Hintergrund. Der Wert
stammt aus Phase 1 und war bis zum 28.08.2026 **geerbt und nie gemessen** —
niemand hatte nachgesehen, ab welcher Grösse ein Lauf wirklich zu lange
dauert (Design-Spec ``2026-08-25-asynchrone-auftraege-design.md`` §11).

Dieses Skript liefert die fehlende Zahl: Sekunden je Dokument, über eine
Reihe von Grössen und über die Formate, die sich in der Verarbeitungsdauer
tatsächlich unterscheiden.

Was gemessen wird — und was ausdrücklich nicht
-----------------------------------------------
Gemessen wird der vollständige Weg von :func:`backend.pipeline.
anonymize_document`: Segmente holen, Erkennung über alle Segmente,
Ersetzungen bauen, formaterhaltend zurückschreiben, Metadaten streichen.
Also genau das, was im synchronen Request zwischen Upload und Antwort
passiert.

**Nicht** gemessen sind drei Dinge, und das ist wichtig für die Auslegung:

* **Der Upload selbst.** Die Datei muss erst durch den Balancer, und dessen
  Zeitgrenze läuft ab dem Verbindungsaufbau, nicht ab dem Verarbeitungsstart.
  Bei einer langsamen Leitung geht davon ein spürbarer Teil des Zeitbudgets
  weg, bevor diese Messung überhaupt beginnt.
* **Die KI-Schicht** (``ki_pruefung``). Sie ist ab Werk aus und hängt an
  einem fremden Dienst; sie in eine Grundlagenmessung zu nehmen hiesse, die
  Antwortzeit von Infomaniak in unsere Schwelle einzubauen. Wer mit
  gesetztem Häkchen arbeitet, hat mehr Zeitbedarf als hier steht, nicht
  weniger.
* **Die Maschine.** Gemessen wird dort, wo das Skript läuft. Der
  Jelastic-Knoten ist nicht dieselbe Maschine; die Zahlen sind als
  Grössenordnung und als Verhältnis zwischen den Formaten zu lesen, nicht
  als Absolutwert für die Produktion. Deshalb stehen zwei Bezugswerte
  daneben, an denen sich Läufe auf verschiedenen Maschinen vergleichen
  lassen: ``s/MiB`` (die Einheit, in der die Schwelle denkt) und
  ``s/1000 Z`` (die Einheit, an der die Arbeit tatsächlich hängt — der
  einzige der beiden, der auch zwischen Formaten vergleichbar ist).

Warum Bytes die schlechtere Grösse sind
----------------------------------------
Die Arbeit hängt am **Text**, nicht an der Dateigrösse: je Segment ein
spaCy-Lauf. Zwischen beiden liegt der Formatanteil, und der ist gewaltig.
Dieselben 64'107 Zeichen wogen im Lauf vom 2026-08-28 als txt 64 KiB, als
docx 39 KiB und als xlsx 8 KiB — Faktor acht. Die Dichte steigt zudem mit
der Dokumentgrösse, weil sich die Formatgrundlast amortisiert (ein docx
trägt rund 36 KiB Vorlage, Thema und Schriften ohne ein Zeichen Text).

**Hier stand zunächst das Gegenteil**, nämlich Bytes seien trotzdem die
richtige Einheit: Die Schwelle müsse entscheiden, *bevor* die Datei
geöffnet wird, und ein Adapterlauf zum Abschätzen der Textmenge wäre selbst
schon ein Teil der teuren Arbeit. Die Spalte ``Auszug%`` widerlegt das —
``extract_segments`` kostet 0.0 bis 4.9 % der Gesamtdauer, und der Anteil
*fällt* mit der Dokumentgrösse. Genau bei den Dateien, bei denen die
Entscheidung zählt, ist das Öffnen fast gratis.

Die Zeichenzahl nach dem Auszug wäre damit das scharfe Kriterium. Ob darauf
umgestellt wird, ist eine Entscheidung über das Verhalten des
Produktivsystems und steht nicht in diesem Skript; die Abwägung samt
Nebenwirkungen steht in
``docs/superpowers/specs/2026-08-25-asynchrone-auftraege-design.md`` §11.
Solange die Schwelle in Bytes prüft, gilt für ihren Wert: am dichtesten
Fall auslegen — und wissen, dass es diesen dichtesten Fall nicht gibt,
sondern nur einen dichtesten *gemessenen*.

Aufruf
------
    .venv/bin/python scripts/schwelle_messen.py
    .venv/bin/python scripts/schwelle_messen.py --budget 30 --format txt
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MIB = 1024 * 1024

#: Textmengen, bei denen gemessen wird — in **Zeichen**, nicht in Bytes.
#:
#: Die Achse war zunächst die Dateigrösse, und das ging schief. Ein
#: Dokument auf eine Zielgrösse zu bringen heisst, gegen den ZIP-Container zu
#: iterieren, und der komprimiert Fliesstext so gut, dass die Paketgrösse der
#: Textmenge kaum folgt: Am 2026-08-28 ergaben 16'579 und 32'980 Zeichen
#: dieselben 37 KiB, und bei 64 KiB Ziel lief die Iteration in ihre
#: Wiederholungsgrenze und lieferte 766'132 Zeichen. Die x-Achse stand still,
#: während die y-Achse stieg.
#:
#: Gemessen wird deshalb über die Textmenge, und die **Dateigrösse ist ein
#: Messergebnis**. Das ist ohnehin die ehrlichere Richtung: Die Arbeit hängt
#: am Text, und wie viele Bytes daraus werden, entscheidet das Format.
#: Die Umrechnung, die die Schwelle braucht, steht dann als gemessene Dichte
#: (Zeichen je KiB) in der Tabelle — je Format verschieden, und genau das ist
#: der Befund.
ZEICHENMENGEN = [
    8_000,
    16_000,
    32_000,
    64_000,
    128_000,
    256_000,
]

#: Zeitbudget des synchronen Pfads in Sekunden.
#:
#: **Seit dem 2026-08-28 gemessen, nicht mehr angenommen.** Hier stand vorher
#: 60.0 — ``proxy_read_timeout``s NGINX-Vorgabe und die plausibelste Annahme,
#: aber eben eine Annahme. Gemessen wurde gegen die Produktion über
#: ``/api/ext/v1`` mit einem befristeten Schlüssel und synthetischen
#: Dokumenten (Verfahren: Design-Spec §12, Variante ohne Umkonfiguration):
#:
#: * 280'000 Zeichen → sauberer ``504`` nach **300.5 s**
#: * 270'000 Zeichen → ``200`` nach 63 s / 124 s / 127 s (drei Läufe)
#: * 150'000–250'000 Zeichen → ``200`` nach 36 s bis 81 s
#:
#: Der Balancer wartet also **300 Sekunden**, das Fünffache der Annahme.
#:
#: Zwei Dinge, die man beim Rechnen damit wissen muss:
#:
#: 1. **Die Streuung ist gross.** Dieselben 270'000 Zeichen brauchten je nach
#:    Lauf 63 bis 127 Sekunden — Faktor zwei, allein durch die Konkurrenz um
#:    die zwei uvicorn-Worker. Der Durchsatz lag bei rund 2'100 bis 4'300
#:    Zeichen/s und damit deutlich unter den 20'000/s, mit denen die Spec
#:    gerechnet hatte.
#: 2. **Gemessen wurde mit einem einzelnen Aufrufer.** Zwei gleichzeitige
#:    grosse Läufe teilen sich dieselben zwei Worker.
#:
#: Der Sicherheitsfaktor darauf ist kein Zierrat: Der Upload zählt mit, der
#: Knoten teilt sich die CPU mit einem zweiten uvicorn-Worker, und ein
#: Dokument kann dichter sein als das gemessene.
BUDGET_SEKUNDEN = 300.0
SICHERHEITSFAKTOR = 3.0


@dataclass
class Messpunkt:
    """Ein Messwert: ein Format in einer Groesse, einmal durchgelaufen."""

    format: str
    bytes: int
    zeichen: int
    segmente: int
    sekunden: float
    #: Nur der Adapterlauf (``extract_segments``) — siehe :meth:`auszug_anteil`.
    auszug_sekunden: float
    funde: int

    @property
    def zeichen_je_kib(self) -> float:
        """Wie viel Text in einem KiB Datei steckt — die Dichte des Formats.

        Die unangenehme Grösse dieser Messung: Sie schwankt zwischen den
        Formaten um mehr als eine Grössenordnung (reiner Text traegt rund ein
        Zeichen je Byte, ein docx durch die ZIP-Kompression ein Vielfaches).
        Genau deshalb kann eine Schwelle in Bytes nie scharf sein.
        """
        return self.zeichen / (self.bytes / 1024)

    @property
    def auszug_anteil(self) -> float:
        """Anteil des Adapterlaufs an der Gesamtdauer, in Prozent.

        Steht hier, weil er eine Annahme widerlegen oder stuetzen kann, die
        im Kopf dieser Datei steht: Die Schwelle prueft Bytes, weil sie
        entscheiden muss, BEVOR die Datei geoeffnet wird. Ist der
        Adapterlauf gemessen billig, waere die Zeichenzahl nach dem Auszug
        die scharfe Alternative zur unscharfen Byte-Schwelle — und die
        Begruendung im Kopf haette ein Loch.
        """
        return self.auszug_sekunden / max(self.sekunden, 1e-9) * 100

    @property
    def sekunden_je_mib(self) -> float:
        """Sekunden je MiB Datei — die Groesse, in der die Schwelle denkt."""
        return self.sekunden / (self.bytes / MIB)

    @property
    def sekunden_je_1000_zeichen(self) -> float:
        """Sekunden je 1000 Zeichen Text — die Groesse, an der die Arbeit haengt.

        Der ehrlichere der beiden Werte, und der einzige, der zwischen
        Formaten vergleichbar ist. ``sekunden_je_mib`` haengt daneben am
        Formatanteil: Ein docx traegt rund 30 KiB Grundlast (Vorlage, Thema,
        Schriften) ohne ein Zeichen Text, was den Wert bei kleinen Dateien
        kuenstlich drueckt.
        """
        return self.sekunden / max(self.zeichen, 1) * 1000


# ── Dokumente bauen ────────────────────────────────────────────────────────
#
# Frei erfundene Namen und Sachverhalte, echte Umlaute (das deutsche Modell
# hält "Spaeter" fälschlich für einen Personennamen, "Später" nicht — siehe
# golden-set/README.md). Kein Satz stammt aus einem echten PTI-Dokument.

_ABSAETZE = [
    "Die Einvernahme von Andrea Beispiel fand am Dienstagvormittag im "
    "Beisein von Rechtsanwalt Marco Lüthi statt. Die beschuldigte Person "
    "wohnt an der Bahnhofstrasse 12 in 3011 Bern und ist unter der Nummer "
    "079 555 44 33 erreichbar.",
    "Der Sachverhalt wurde durch die Kantonspolizei Freiburg erhoben. "
    "Zuständig für das weitere Verfahren ist Staatsanwältin Nadia Zbinden, "
    "erreichbar über nadia.zbinden@example.ch.",
    "Gemäss dem Bericht vom Vortag hat sich der Vorfall gegen 14:30 Uhr "
    "ereignet. Zeugin Simone Kaufmann gab an, zwei Personen beim Verlassen "
    "des Gebäudes gesehen zu haben.",
    "Die Auszahlung erfolgte auf das Konto CH93 0076 2011 6238 5295 7 "
    "lautend auf Peter Amrein. Der Betrag wurde am Folgetag zurückgebucht.",
    "Für Rückfragen steht die Sachbearbeiterin Claudia Steiner zur "
    "Verfügung. Die Akte wird bei der Meldestelle für Geldwäscherei "
    "geführt und trägt die interne Bezeichnung des Regionalgerichts.",
    "Das Fahrzeug mit dem Kontrollschild FR 12345 wurde am Vortag an der "
    "Route des Alpes in Freiburg abgestellt. Der Halter konnte bislang "
    "nicht ermittelt werden.",
]


#: Namen, die beim Wiederholen durchrotieren — gegen die Kompression.
#:
#: Ebenfalls frei erfunden.
_NAMEN = [
    "Andrea Beispiel", "Marco Lüthi", "Nadia Zbinden", "Simone Kaufmann",
    "Peter Amrein", "Claudia Steiner", "Reto Bühlmann", "Yvonne Grädel",
    "Thomas Wildhaber", "Franziska Öttli", "Michel Perroud", "Sarah Küng",
]


def _absaetze_bis(zielzeichen: int) -> List[str]:
    """Baut Absätze bis zur Zielmenge — **mit wechselndem Inhalt**.

    Die erste Fassung wiederholte sechs feste Absätze wörtlich, und daran
    scheiterte die Messung am 2026-08-28 zunächst: Ein ZIP komprimiert
    wörtliche Wiederholung fast vollständig weg. Die doppelte Textmenge
    ergab dieselbe Dateigrösse — und damit lief die Messung gegen eine
    x-Achse, die sich nicht bewegte, während die y-Achse stieg. Genau die
    Beziehung, die gemessen werden soll, war dadurch zerstört.

    Deshalb trägt jeder Absatz eine laufende Nummer und einen wechselnden
    Namen. Ein echtes Dokument ist so beschaffen: kein Absatz gleicht dem
    anderen, das Paket komprimiert um Faktor vier bis fünf und nicht um
    zwanzig.
    """
    text: List[str] = []
    laenge = 0
    i = 0
    while laenge < zielzeichen:
        vorlage = _ABSAETZE[i % len(_ABSAETZE)]
        # Aktenzeichen und ein rotierender Name je Absatz: genug Entropie,
        # dass der ZIP-Container realistisch komprimiert, ohne den Satzbau
        # zu zerstoeren (das Sprachmodell soll echten Text sehen).
        absatz = (
            f"Ziffer {i + 1}, Akte 2026-{100000 + i}: "
            f"{vorlage} Zuständig war {_NAMEN[i % len(_NAMEN)]}."
        )
        text.append(absatz)
        laenge += len(absatz)
        i += 1
    return text


def baue_txt(zielzeichen: int) -> bytes:
    """Reiner Text — der dichteste Fall: fast jedes Byte ist Text."""
    return "\n\n".join(_absaetze_bis(zielzeichen)).encode("utf-8")


def baue_docx(zielzeichen: int) -> bytes:
    """Ein docx, dessen Grösse fast ausschliesslich aus Fliesstext kommt."""
    from docx import Document

    dokument = Document()
    for absatz in _absaetze_bis(zielzeichen):
        dokument.add_paragraph(absatz)
    puffer = io.BytesIO()
    dokument.save(puffer)
    return puffer.getvalue()


def baue_xlsx(zielzeichen: int) -> bytes:
    """Eine Arbeitsmappe mit einer Textspalte — mehr Segmente je Zeichen.

    Der interessante Unterschied zu docx: Ein Segment ist hier eine **Zeile**
    (der Adapter klebt die Zellen einer Zeile mit Tabulator zusammen, siehe
    ``backend/adapters/xlsx_adapter.py``). Bei gleicher Zeichenzahl entstehen
    dadurch andere Segmentzuschnitte als bei einem Absatzdokument — und weil
    je Segment ein spaCy-Lauf anfällt, ist das genau die Grösse, an der die
    Dauer hängt.

    Gebaut über ``backend.tests.ooxml_fabrik.baue_xlsx``, dieselbe Fabrik,
    die auch das Golden-Set benutzt: Ein zweiter, hier hingeschriebener
    OOXML-Baukasten wäre eine Fehlerquelle, die nichts misst.
    """
    from backend.tests.ooxml_fabrik import baue_xlsx as fabrik

    zeilen = [[absatz] for absatz in _absaetze_bis(zielzeichen)]
    return fabrik([("Blatt1", zeilen)])


BAUER: dict[str, tuple[Callable[[int], bytes], str]] = {
    "txt": (baue_txt, "messung.txt"),
    "docx": (baue_docx, "messung.docx"),
    "xlsx": (baue_xlsx, "messung.xlsx"),
}


# ── Messen ─────────────────────────────────────────────────────────────────


def _session():
    """Eine Wegwerf-Datenbank im Speicher.

    ``anonymize_document`` verlangt eine Session, benutzt sie im hier
    gemessenen Modus (``vault=None``, also anonym) aber nicht: Ohne Tresor
    wird nur der Typ eingesetzt und nichts gespeichert. Gemessen wird damit
    die Engine und nicht die Datenbank — was richtig ist, denn der
    Tresorschreibweg hängt an der Fundzahl und nicht an der Dateigrösse.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.vault.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def miss(formatname: str, zielzeichen: int) -> Messpunkt:
    """Baut ein Dokument dieser Textmenge und laesst es einmal durchlaufen."""
    from backend.adapters import get_adapter
    from backend.pipeline import anonymize_document

    bauer, dateiname = BAUER[formatname]
    daten = bauer(zielzeichen)

    adapter = get_adapter(dateiname)
    beginn_auszug = time.perf_counter()
    segmente = adapter.extract_segments(daten)
    auszug = time.perf_counter() - beginn_auszug
    zeichen = sum(len(s.text) for s in segmente)

    session = _session()
    beginn = time.perf_counter()
    _, befund, _ = anonymize_document(
        session, None, daten, dateiname, ki_pruefung=False
    )
    dauer = time.perf_counter() - beginn
    session.close()

    return Messpunkt(
        format=formatname,
        bytes=len(daten),
        zeichen=zeichen,
        segmente=len(segmente),
        sekunden=dauer,
        auszug_sekunden=auszug,
        funde=len(befund.findings),
    )


def empfehlung(punkte: List[Messpunkt], budget: float) -> tuple[float, float, int]:
    """Leitet aus den Messpunkten eine Schwelle in **Bytes** ab.

    Zwei Grössen, beide aus der Messung, beide im ungünstigsten gemessenen
    Fall genommen — die Schwelle muss den schlechten Fall tragen, sonst
    schützt sie genau dann nicht, wenn es darauf ankommt:

    * **Sekunden je 1000 Zeichen**: wie teuer der Text ist. Nahezu konstant,
      also die verlässliche Hälfte der Rechnung.
    * **Zeichen je KiB Datei**: wie viel Text sich hinter einem Byte
      verstecken kann. Die unangenehme Hälfte — sie schwankt zwischen den
      Formaten um mehr als eine Grössenordnung, und sie ist der Grund, warum
      eine Byte-Schwelle nie scharf sein kann.
    """
    teuerster_text = max(p.sekunden_je_1000_zeichen for p in punkte)
    dichtestes = max(p.zeichen_je_kib for p in punkte)
    tragbare_zeichen = (budget / SICHERHEITSFAKTOR) / teuerster_text * 1000
    return teuerster_text, dichtestes, int(tragbare_zeichen / dichtestes * 1024)


def main() -> int:
    """Misst alle gewaehlten Formate und gibt Tabelle samt Empfehlung aus."""
    zerleger = argparse.ArgumentParser(description=__doc__)
    zerleger.add_argument(
        "--budget", type=float, default=BUDGET_SEKUNDEN,
        help="Zeitbudget des synchronen Pfads in Sekunden (Vorgabe 60).",
    )
    zerleger.add_argument(
        "--format", action="append", choices=sorted(BAUER),
        help="Nur dieses Format messen (mehrfach angebbar).",
    )
    zerleger.add_argument(
        "--bis", type=int, default=256_000,
        help="Grösste zu messende Textmenge in Zeichen (Vorgabe 256'000).",
    )
    argumente = zerleger.parse_args()

    formate = argumente.format or sorted(BAUER)
    mengen = [z for z in ZEICHENMENGEN if z <= argumente.bis]

    # Erst warmlaufen, dann messen. Das spaCy-Modell wird beim ersten
    # ``analyze`` geladen (rund eine Sekunde), und ohne diesen Leerlauf
    # steckte diese Ladezeit vollstaendig im ERSTEN Messpunkt. Beim Lauf am
    # 2026-08-28 sah man es deutlich: 0.146 s/1000 Zeichen im ersten Punkt
    # gegen 0.048 im zweiten, bei identischem Dokumenttyp. Weil die
    # Empfehlung unten den LANGSAMSTEN Punkt nimmt, haette allein die
    # Ladezeit die Schwelle bestimmt.
    print("Warmlauf …", flush=True)
    miss("txt", 8_000)

    print("\nMessung der Verarbeitungsdauer je Textmenge\n")
    print(
        f"{'Format':<6} {'Grösse':>8} {'Zeichen':>10} {'Segmente':>9} "
        f"{'Sekunden':>9} {'s/MiB':>8} {'s/1000 Z':>9} {'Z/KiB':>8} "
        f"{'Auszug%':>8} {'Funde':>7}"
    )
    print("-" * 92)

    punkte: List[Messpunkt] = []
    for formatname in formate:
        for menge in mengen:
            punkt = miss(formatname, menge)
            punkte.append(punkt)
            print(
                f"{punkt.format:<6} {punkt.bytes / 1024:>7,.0f}K "
                f"{punkt.zeichen:>10,} {punkt.segmente:>9,} "
                f"{punkt.sekunden:>9.2f} {punkt.sekunden_je_mib:>8.1f} "
                f"{punkt.sekunden_je_1000_zeichen:>9.3f} "
                f"{punkt.zeichen_je_kib:>8,.0f} {punkt.auszug_anteil:>7.1f}% "
                f"{punkt.funde:>7,}",
                flush=True,
            )

    teuerster_text, dichtestes, schwelle = empfehlung(punkte, argumente.budget)
    tragbare_zeichen = (argumente.budget / SICHERHEITSFAKTOR) / teuerster_text * 1000
    print()
    print(f"Teuerster gemessener Text    : {teuerster_text:.3f} s je 1000 Zeichen")
    print(f"Dichtestes gemessenes Format : {dichtestes:,.0f} Zeichen je KiB")
    print(f"Zeitbudget (Annahme)         : {argumente.budget:.0f} s")
    print(f"Sicherheitsfaktor            : {SICHERHEITSFAKTOR:.0f}x")
    print(f"Tragbare Textmenge           : {tragbare_zeichen:,.0f} Zeichen")
    print(
        f"Daraus Schwelle in Bytes     : {schwelle:,} Byte "
        f"({schwelle / 1024:,.0f} KiB)"
    )
    print()
    print(
        "ACHTUNG: Die Byte-Schwelle oben ist NICHT stabil. Sie faellt weiter,\n"
        "je groessere Dokumente man misst, weil sich die Formatgrundlast\n"
        "amortisiert und die Dichte (Z/KiB) dabei steigt. Belastbar ist nur\n"
        "die tragbare TEXTMENGE; ihre Umrechnung in Bytes ist eine Schaetzung\n"
        "ueber ein Verhaeltnis, das je Format um mehr als das Achtfache\n"
        "schwankt. Siehe §11 des Design-Spec zu den asynchronen Auftraegen."
    )
    print()
    print(
        "Die Zahlen gelten fuer die Maschine, auf der dieses Skript lief.\n"
        "Auf dem Jelastic-Knoten neu messen, bevor die Schwelle geaendert wird."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
