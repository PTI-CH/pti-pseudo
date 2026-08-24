"""Erzeugt das synthetische Golden-Set des PTI-Pseudonymisierers.

Ein Golden-Set ist eine Sammlung von Testdokumenten, bei denen *vorher*
feststeht, was die Erkennung finden **muss** (``erwartet``) und was sie
**nie** finden darf (``verboten``). Damit lässt sich nach jeder Änderung an
den Erkennern messen, ob Recall und Precision besser oder schlechter wurden
— siehe Fachkonzept §13.1/§13.2 und ``backend/tests/test_golden_set.py``.

Absolute Regel: **keine echten Personendaten.** Alle Namen, Adressen,
Telefonnummern und Kontonummern in diesem Modul sind frei erfunden. Die
Struktur-PII (AHV, IBAN) besteht zwar die jeweilige Prüfziffernrechnung —
das macht sie erkennbar, nicht real. E-Mail-Adressen liegen ausschliesslich
unter ``example.ch``/``example.com`` (RFC 2606 reserviert diese Domains
genau für solche Zwecke).

Echte Umlaute
--------------
Alle Dokumenttexte verwenden echte Umlaute (ä/ö/ü/ß), nie eine
ae/oe/ue-Transliteration. Das deutsche spaCy-Modell hält „Spaeter"
fälschlich für einen Personennamen, „Später" nicht — ein Golden-Set in
transliterierter Schreibweise würde also eine andere (schlechtere) Grösse
messen als die, die im echten Betrieb vorkommt.

PTI-Fachvokabular
------------------
Die Konstanten ``_BEHOERDEN``, ``_ABKUERZUNGEN`` und ``_GESETZE`` unten sind
eine kleine, feste Auswahl aus dem Glossar des Schwestertools *Translator*
(geprüft: enthält keine Personennamen). Sie dienen zwei Zwecken:

* **Behörden** in ausgeschriebener Form (``"Kantonspolizei Freiburg"``,
  ``"Meldestelle für Geldwäscherei"`` …) sind echte ``ORGANIZATION``-Treffer
  — sie tauchen in ``erwartet`` auf.
* **Abkürzungen** (``GS``, ``AM``, ``MG``, ``WG`` …) sehen wie Initialen
  oder Kantonskürzel aus und sind gezielte Fehlalarm-Fallen — sie tauchen in
  ``verboten`` auf, mit ``entity_type="PERSON"``.

Struktur-PII: echt, aber erfunden
-----------------------------------
AHV-Nummer und IBAN werden nicht als Zufallsziffern hingeschrieben, sondern
mit den echten Prüfziffernverfahren berechnet
(:func:`backend.engine.recognizers.ahv.compute_check_digit` bzw. einer
lokalen Mod-97-Implementierung) und anschliessend mit den
Validierungsfunktionen des Projekts verifiziert — siehe die Assertions
gleich nach der Berechnung unten. Nur eine gültige Prüfziffer macht die
Nummer für ``SwissAhvRecognizer``/``SwissIbanRecognizer`` überhaupt sichtbar.

Fussnote ohne ``python-docx``
-------------------------------
``python-docx`` kann Kommentare erzeugen (``Document.add_comment``), aber
**keine Fussnoten** — dafür gibt es in Version 1.2.0 keine öffentliche API.
:func:`_fussnote_einfuegen` behilft sich deshalb auf zwei Ebenen:

1. Der Fussnotenanker (``w:footnoteReference``) wird direkt als
   ``lxml``-Element in den Absatz eingehängt, **bevor** das Dokument
   gespeichert wird (``paragraph._p.append(...)``) — ``python-docx`` reicht
   das beim Speichern unverändert durch.
2. Der Fussnotentext selbst existiert in einem eigenen Part
   (``word/footnotes.xml``), den ``python-docx`` nicht kennt. Dieser Part
   wird nach dem Speichern per ``zipfile``/``lxml`` von Hand ins Paket
   eingefügt, inklusive Content-Type-Override und Beziehung — siehe
   :func:`_fussnote_einfuegen`.

Genau dieser Part ist der Testfall für den generischen lxml-Durchlauf von
``DocxAdapter``: ``python-docx`` beschriftet ihn nicht (kein Label aus
``_labels_from_python_docx``), er taucht deshalb mit der ID
``fallback:word/footnotes.xml/p<i>`` auf.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from docx import Document
from docx.oxml.ns import qn
from docx.oxml.shared import OxmlElement
from lxml import etree

#: Repo-Wurzel, damit dieses Modul auch als eigenständiges Skript
#: (``python golden-set/synthetic/generator.py``) die ``backend``-Pakete
#: findet, unabhängig vom aktuellen Arbeitsverzeichnis.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.engine.recognizers.ahv import (  # noqa: E402
    compute_check_digit,
    format_ahv,
    is_valid_ahv,
)
from backend.engine.recognizers.iban_ch import is_valid_swiss_iban  # noqa: E402
from backend.engine.recognizers.phone_ch import is_valid_swiss_phone  # noqa: E402

__all__ = ["Erwartung", "Testdokument", "DOKUMENTE", "schreibe_dateien"]


# ── Vertrag ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Erwartung:
    """Ein einzelner erwarteter oder verbotener Fund im Golden-Set.

    Attributes
    ----------
    text
        Exakter Textausschnitt, wie er im extrahierten Segmenttext des
        Adapters vorkommen muss (``(text, entity_type)`` ist der
        Vergleichsschlüssel in ``scripts/golden_set_report.py``).
    entity_type
        Erwarteter bzw. verbotener Entitätstyp, z. B. ``"PERSON"``.
    grund
        Nur bei ``verboten`` befüllt: warum dieser Fund kein echter Treffer
        wäre.
    """

    text: str
    entity_type: str
    grund: str = ""


@dataclass(frozen=True)
class Testdokument:
    """Ein einzelnes Golden-Set-Dokument samt Erwartungen.

    Attributes
    ----------
    name
        Kurzer, dateisystemtauglicher Bezeichner, z. B.
        ``"protokoll_mehrere_runs"``.
    dateiname
        Dateiname inklusive Endung — die Endung bestimmt, welcher Adapter
        das Dokument liest (``get_adapter(dateiname)``).
    beschreibung
        Ein Satz, was dieses Dokument prüft.
    inhalt
        Die fertigen Dokumentbytes, deterministisch im Speicher erzeugt.
    erwartet
        Funde, die die Erkennung liefern **muss**.
    verboten
        Funde, die die Erkennung **nie** liefern darf.
    begriffe
        Einträge der Begriffsliste (Ebene 0), die für dieses Dokument
        mitgegeben werden. Leer bei den meisten Dokumenten.

        Diese Ebene braucht eigene Testfälle, weil sie sich grundlegend von
        den übrigen unterscheidet: Behörden-, Projekt- und Produktnamen sind
        an *nichts* erkennbar ausser daran, dass jemand sie eingetragen hat.
        Kein Muster, keine Prüfziffer, kein Sprachmodell findet sie.

        Ohne solche Fälle wäre eine geleerte Begriffsliste im Betrieb
        unsichtbar: die Messung liefe weiterhin grün, während die Erkennung
        still schlechter geworden wäre.
    """

    name: str
    dateiname: str
    beschreibung: str
    inhalt: bytes
    erwartet: tuple[Erwartung, ...]
    verboten: tuple[Erwartung, ...]
    begriffe: tuple[dict, ...] = ()


# ── PTI-Fachvokabular (Auszug aus dem Translator-Glossar) ──────────────────
#
# Nur ``kurz``/``voll``-Paare, die unten tatsächlich in einem Dokument
# verwendet werden. Quelle: Glossar-Export des Schwestertools Translator,
# geprüft auf Abwesenheit von Personennamen.

#: Behörden, ausgeschrieben — echte ``ORGANIZATION``-Treffer.
_BAKOM = "Bundesamt für Kommunikation"
_FEDPOL = "Bundesamt für Polizei"
_EJPD = "Eidgenössisches Justiz- und Polizeidepartement"
_MELDESTELLE_GWG = "Meldestelle für Geldwäscherei"
#: Nicht wörtlich im Glossar-Auszug, aber derselbe reale Behördentyp
#: (Kantonspolizei + Kantonskürzel) — realistisches PTI-Fachvokabular.
_KAPO_FR = "Kantonspolizei Freiburg"

#: Frei erfundener Lieferantenname für ``_baue_wiederholte_nennung``.
#: Bewusst so gewählt, dass das Modell ihn im ausformulierten Satz als
#: Organisation erkennt, kontextlos aber übersieht — nur dann misst das
#: Dokument, was es messen soll. Wird er ausgetauscht, ist dieses Verhalten
#: neu zu prüfen, sonst misst der Testfall nichts mehr.
_LIEFERANT = "Talblick"

#: Abkürzungen aus dem Glossar — gezielte Fehlalarm-Fallen. Sehen wie
#: Initialen oder Aktenzeichen aus, sind aber Institutions- bzw.
#: Erlasskürzel und dürfen nie als PERSON oder CH_PLATE durchgehen.
_ABKUERZUNG_GS = "GS"  # Generalsekretariat (Behörde)
_ABKUERZUNG_AM = "AM"  # Account Manager (Abkürzung)
_ABKUERZUNG_MG = "MG"  # Bundesgesetz über die Armee und die Militärverwaltung
_ABKUERZUNG_WG = "WG"  # Bundesgesetz über Waffen, Waffenzubehör und Munition

#: Gesetzeskürzel, nur als Fliesstext-Würze im PTI-Protokoll — nicht
#: bewertet (DATE_TIME/Erlasskürzel gehören nicht zu BEWERTETE_TYPEN).
_DSG = "Bundesgesetz über den Datenschutz"
_GWG = "Geldwäschereigesetz"
_BUEPF = "Bundesgesetz betreffend die Überwachung des Post- und Fernmeldeverkehrs"


# ── Struktur-PII: berechnet und verifiziert ────────────────────────────────


def _berechne_iban_pruefziffern(laendercode: str, bban: str) -> str:
    """Berechnet die IBAN-Prüfziffern nach ISO 7064 (Mod-97-10).

    Eigenständige Umsetzung (statt eines Imports aus
    ``backend.engine.recognizers.iban_ch``, das nur validiert, nicht
    berechnet): Land und BBAN werden ans Ende verschoben, Buchstaben durch
    ``A``=10…``Z``=35 ersetzt, der Rest modulo 97 gebildet und die
    Prüfziffer als ``98 - Rest`` bestimmt. Das Ergebnis wird unten sofort
    mit :func:`is_valid_swiss_iban` verifiziert — hier steht keine
    unbestätigte Berechnung.
    """
    umgestellt = bban + laendercode + "00"
    ziffern = "".join(
        str(ord(zeichen) - 55) if zeichen.isalpha() else zeichen
        for zeichen in umgestellt
    )
    rest = int(ziffern) % 97
    return f"{98 - rest:02d}"


def _iban_gruppen(iban_kompakt: str) -> str:
    """Formatiert eine kompakte IBAN in Vierergruppen (Rechnungsschreibweise)."""
    return " ".join(iban_kompakt[i : i + 4] for i in range(0, len(iban_kompakt), 4))


# AHV: 756 + neun frei erfundene Ziffern, Prüfziffer per EAN-13 berechnet.
_AHV_ERSTE_ZWOELF = "756" + "481523697"
_AHV_KOMPAKT = _AHV_ERSTE_ZWOELF + str(compute_check_digit(_AHV_ERSTE_ZWOELF))
assert is_valid_ahv(_AHV_KOMPAKT), "Golden-Set-AHV-Nummer besteht die Prüfziffer nicht"
AHV_NUMMER = format_ahv(_AHV_KOMPAKT)

# IBAN: fiktives Bankclearing + Kontonummer, Prüfziffern per Mod-97 berechnet.
_IBAN_BBAN = "00762" + "011234567801"  # 5 Ziffern Clearing + 12 Zeichen Konto
_IBAN_KOMPAKT = "CH" + _berechne_iban_pruefziffern("CH", _IBAN_BBAN) + _IBAN_BBAN
assert is_valid_swiss_iban(_IBAN_KOMPAKT), (
    "Golden-Set-IBAN besteht die Mod-97-Prüfung nicht"
)
IBAN_NUMMER = _iban_gruppen(_IBAN_KOMPAKT)

# Telefon: Gebietskennzahlen (Mobile 79, Festnetz 31) direkt gegen den
# Nummernraum des Projekts verifiziert statt geraten.
TELEFON_MOBIL = "079 123 45 67"
TELEFON_FESTNETZ = "031 300 45 67"
assert is_valid_swiss_phone(TELEFON_MOBIL), "Mobilnummer liegt ausserhalb des Plans"
assert is_valid_swiss_phone(TELEFON_FESTNETZ), (
    "Festnetznummer liegt ausserhalb des Plans"
)


# ── DOCX-Hilfsfunktionen ─────────────────────────────────────────────────


def _als_bytes(dokument: Document) -> bytes:
    """Serialisiert ein ``python-docx``-Dokument in Bytes, ohne Datei auf Platte."""
    puffer = io.BytesIO()
    dokument.save(puffer)
    return puffer.getvalue()


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_FOOTNOTES_RELTYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
)
_FOOTNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)


def _fussnotenanker_einhaengen(paragraph, fussnoten_id: str) -> None:
    """Hängt einen ``w:footnoteReference``-Anker ans Ende eines Absatzes.

    Reine ``lxml``-Manipulation am ``python-docx``-Absatzelement, **vor**
    dem Speichern des Dokuments — siehe Modul-Docstring, Abschnitt „Fussnote
    ohne python-docx".
    """
    lauf = OxmlElement("w:r")
    lauf_eigenschaften = OxmlElement("w:rPr")
    stil = OxmlElement("w:rStyle")
    stil.set(qn("w:val"), "FootnoteReference")
    lauf_eigenschaften.append(stil)
    lauf.append(lauf_eigenschaften)
    referenz = OxmlElement("w:footnoteReference")
    referenz.set(qn("w:id"), fussnoten_id)
    lauf.append(referenz)
    paragraph._p.append(lauf)  # noqa: SLF001 - gezielter oxml-Zugriff, siehe Docstring


def _fussnote_einfuegen(paket_bytes: bytes, fussnoten_id: str, text: str) -> bytes:
    """Fügt ``word/footnotes.xml`` nachträglich in ein gespeichertes DOCX ein.

    ``python-docx`` kennt keinen Fussnoten-Part; dieser Schritt baut ihn von
    Hand über ``zipfile``/``lxml`` nach (Content-Type-Override in
    ``[Content_Types].xml``, Beziehung in ``word/_rels/document.xml.rels``,
    neuer Zip-Eintrag ``word/footnotes.xml``). Der zugehörige Anker im
    Fliesstext muss vorher bereits über :func:`_fussnotenanker_einhaengen`
    im Dokument stehen.

    Parameters
    ----------
    paket_bytes
        Bytes eines bereits von ``python-docx`` gespeicherten ``.docx``.
    fussnoten_id
        ``w:id`` der Fussnote — muss mit dem zuvor eingehängten Anker
        übereinstimmen.
    text
        Sichtbarer Fussnotentext (darf Personendaten enthalten, das ist
        genau der Testfall).

    Returns
    -------
    bytes
        Das Paket mit eingefügtem Fussnoten-Part.
    """
    with zipfile.ZipFile(io.BytesIO(paket_bytes)) as archiv:
        infos = list(archiv.infolist())
        blobs = {info.filename: archiv.read(info.filename) for info in infos}

    fussnoten_wurzel = etree.Element(f"{{{_W_NS}}}footnotes", nsmap={"w": _W_NS})

    def _neue_fussnote(id_wert: str, typ: str | None = None) -> etree._Element:
        knoten = etree.SubElement(fussnoten_wurzel, f"{{{_W_NS}}}footnote")
        knoten.set(f"{{{_W_NS}}}id", id_wert)
        if typ is not None:
            knoten.set(f"{{{_W_NS}}}type", typ)
        return knoten

    # Trenn- und Fortsetzungstrenner: Word erwartet sie, auch wenn dieses
    # Golden-Set-Dokument sie nie sichtbar rendert.
    for id_wert, typ, marker_tag in (
        ("-1", "separator", "separator"),
        ("0", "continuationSeparator", "continuationSeparator"),
    ):
        fussnote = _neue_fussnote(id_wert, typ)
        absatz = etree.SubElement(fussnote, f"{{{_W_NS}}}p")
        lauf = etree.SubElement(absatz, f"{{{_W_NS}}}r")
        etree.SubElement(lauf, f"{{{_W_NS}}}{marker_tag}")

    echte_fussnote = _neue_fussnote(fussnoten_id)
    absatz = etree.SubElement(echte_fussnote, f"{{{_W_NS}}}p")
    marker_lauf = etree.SubElement(absatz, f"{{{_W_NS}}}r")
    marker_eigenschaften = etree.SubElement(marker_lauf, f"{{{_W_NS}}}rPr")
    etree.SubElement(marker_eigenschaften, f"{{{_W_NS}}}rStyle").set(
        f"{{{_W_NS}}}val", "FootnoteReference"
    )
    etree.SubElement(marker_lauf, f"{{{_W_NS}}}footnoteRef")
    text_lauf = etree.SubElement(absatz, f"{{{_W_NS}}}r")
    text_knoten = etree.SubElement(text_lauf, f"{{{_W_NS}}}t")
    text_knoten.set(f"{{{_XML_NS}}}space", "preserve")
    text_knoten.text = text

    footnotes_xml = etree.tostring(
        fussnoten_wurzel, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    content_types_wurzel = etree.fromstring(blobs["[Content_Types].xml"])
    override = etree.SubElement(content_types_wurzel, f"{{{_CT_NS}}}Override")
    override.set("PartName", "/word/footnotes.xml")
    override.set("ContentType", _FOOTNOTES_CONTENT_TYPE)
    content_types_xml = etree.tostring(
        content_types_wurzel, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    beziehungen_wurzel = etree.fromstring(blobs["word/_rels/document.xml.rels"])
    vorhandene_ids = [
        int(knoten.get("Id")[3:])
        for knoten in beziehungen_wurzel
        if (knoten.get("Id") or "").startswith("rId")
        and knoten.get("Id")[3:].isdigit()
    ]
    neue_id = f"rId{max(vorhandene_ids, default=0) + 1}"
    beziehung = etree.SubElement(beziehungen_wurzel, f"{{{_REL_NS}}}Relationship")
    beziehung.set("Id", neue_id)
    beziehung.set("Type", _FOOTNOTES_RELTYPE)
    beziehung.set("Target", "footnotes.xml")
    beziehungen_xml = etree.tostring(
        beziehungen_wurzel, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    blobs["word/footnotes.xml"] = footnotes_xml
    blobs["[Content_Types].xml"] = content_types_xml
    blobs["word/_rels/document.xml.rels"] = beziehungen_xml
    infos.append(zipfile.ZipInfo("word/footnotes.xml", date_time=infos[0].date_time))

    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as archiv:
        for info in infos:
            archiv.writestr(info, blobs[info.filename])
    return puffer.getvalue()


# ── Dokument 1: Name über mehrere Runs ──────────────────────────────────────


def _baue_mehrere_runs() -> Testdokument:
    """Namen, die Word über mehrere Runs verteilt hat (nachträgliche Bearbeitung)."""
    dokument = Document()

    absatz = dokument.add_paragraph()
    absatz.add_run("Die Meldung wurde von ")
    absatz.add_run("Andrea ")
    absatz.add_run("Mus")
    absatz.add_run("ter")
    absatz.add_run(
        " persönlich unterschrieben und am 12. März 2026 im Archiv abgelegt."
    )

    zweiter_absatz = dokument.add_paragraph()
    zweiter_absatz.add_run("Für Rückfragen steht ")
    zweiter_absatz.add_run("Kevin ")
    zweiter_absatz.add_run("Wid")
    zweiter_absatz.add_run("mer")
    zweiter_absatz.add_run(" während der Bürozeiten zur Verfügung.")

    erwartet = (
        Erwartung("Andrea Muster", "PERSON"),
        Erwartung("Kevin Widmer", "PERSON"),
    )
    return Testdokument(
        name="protokoll_mehrere_runs",
        dateiname="protokoll_mehrere_runs.docx",
        beschreibung=(
            "Namen, die Word beim nachträglichen Bearbeiten in mehrere "
            "w:r-Runs zerlegt hat ('Andrea '/'Mus'/'ter'); nur die "
            "Run-Konkatenation des Adapters findet sie."
        ),
        inhalt=_als_bytes(dokument),
        erwartet=erwartet,
        verboten=(),
    )


# ── Dokument 2: Struktur-PII, alle gültig ───────────────────────────────────


def _baue_struktur_pii() -> Testdokument:
    """AHV, CH-IBAN, Telefon und E-Mail — alle mit gültiger Prüfziffer."""
    dokument = Document()
    dokument.add_paragraph(
        f"Die Sozialversicherungsnummer der betroffenen Person lautet "
        f"{AHV_NUMMER}."
    )
    dokument.add_paragraph(
        f"Rückerstattungen bitte auf die IBAN {IBAN_NUMMER} überweisen."
    )
    dokument.add_paragraph(
        f"Erreichbar unter {TELEFON_MOBIL} (mobil) oder {TELEFON_FESTNETZ} "
        f"(Zentrale)."
    )
    dokument.add_paragraph(
        "Für schriftliche Rückfragen: aktennotiz.fallakte@example.ch."
    )

    erwartet = (
        Erwartung(AHV_NUMMER, "CH_AHV"),
        Erwartung(IBAN_NUMMER, "CH_IBAN"),
        Erwartung(TELEFON_MOBIL, "CH_PHONE"),
        Erwartung(TELEFON_FESTNETZ, "CH_PHONE"),
        Erwartung("aktennotiz.fallakte@example.ch", "EMAIL_ADDRESS"),
    )
    return Testdokument(
        name="struktur_pii_gueltig",
        dateiname="struktur_pii_gueltig.docx",
        beschreibung=(
            "AHV-Nummer und IBAN mit berechneter und verifizierter "
            "Prüfziffer, dazu gültige CH-Telefonnummern und eine "
            "E-Mail-Adresse — Struktur-PII, die 100 % Recall erreichen muss."
        ),
        inhalt=_als_bytes(dokument),
        erwartet=erwartet,
        verboten=(),
    )


# ── Dokument 3: Fehlalarm-Fallen ─────────────────────────────────────────


def _baue_fehlalarm_fallen() -> Testdokument:
    """Zahlen, Beträge und Glossar-Abkürzungen, die nie feuern dürfen."""
    dokument = Document()
    dokument.add_paragraph(
        "Der Vorgang wurde im Jahr 2026 abgeschlossen, nachdem er im Jahr "
        "2024 eröffnet worden war."
    )
    dokument.add_paragraph(
        "Die Rechnung über CHF 8500 wurde beglichen; die Artikelnummer "
        "4712 des Formulars blieb dabei unverändert."
    )
    dokument.add_paragraph(
        f"Zuständig war das {_ABKUERZUNG_GS} in Absprache mit dem "
        f"{_ABKUERZUNG_AM}. Die rechtliche Prüfung erfolgte gestützt auf "
        f"{_ABKUERZUNG_MG} und {_ABKUERZUNG_WG}."
    )

    verboten = (
        Erwartung("2026", "CH_POSTAL_CODE", "Jahreszahl, keine Postleitzahl"),
        Erwartung("2024", "CH_POSTAL_CODE", "Jahreszahl, keine Postleitzahl"),
        Erwartung("8500", "CH_POSTAL_CODE", "Frankenbetrag, keine Postleitzahl"),
        Erwartung("4712", "CH_POSTAL_CODE", "Artikelnummer, keine Postleitzahl"),
        Erwartung(
            _ABKUERZUNG_GS,
            "PERSON",
            "Glossar-Abkürzung (Generalsekretariat), keine Initialen",
        ),
        Erwartung(
            _ABKUERZUNG_AM,
            "PERSON",
            "Glossar-Abkürzung (Account Manager), keine Initialen",
        ),
        Erwartung(
            _ABKUERZUNG_MG,
            "PERSON",
            "Glossar-Abkürzung (Militärgesetz), keine Initialen",
        ),
        Erwartung(
            _ABKUERZUNG_WG,
            "PERSON",
            "Glossar-Abkürzung (Waffengesetz), keine Initialen",
        ),
    )
    return Testdokument(
        name="fehlalarm_fallen",
        dateiname="fehlalarm_fallen.docx",
        beschreibung=(
            "Jahreszahlen, Frankenbeträge, Artikelnummern und "
            "Glossar-Abkürzungen (GS/AM/MG/WG) — sehen wie PLZ oder "
            "Personeninitialen aus, dürfen aber nie als solche gemeldet "
            "werden."
        ),
        inhalt=_als_bytes(dokument),
        erwartet=(),
        verboten=verboten,
    )


# ── Dokument 4: Tabelle, Kopf- und Fusszeile ────────────────────────────────


def _baue_tabelle_kopf_fusszeile() -> Testdokument:
    """PII in Tabellenzellen sowie in Kopf- und Fusszeile."""
    dokument = Document()

    abschnitt = dokument.sections[0]
    abschnitt.header.paragraphs[0].text = f"{_KAPO_FR} – vertraulich"
    abschnitt.footer.paragraphs[0].text = (
        "Diese Fallakte wird von Sandra Zbinden bearbeitet, Rückfragen an "
        "sandra.zbinden@example.ch."
    )

    tabelle = dokument.add_table(rows=1, cols=3)
    tabelle.style = "Table Grid"
    kopfzeile = tabelle.rows[0].cells
    kopfzeile[0].text = "Art der Angabe"
    kopfzeile[1].text = "Angabe"
    kopfzeile[2].text = "Bemerkung"

    zeilen = (
        ("Name", "Michael Steiner", "Verdächtiger"),
        ("Telefon", "044 123 45 67", "privat"),
        ("Wohnort", "Zürich", "Hauptwohnsitz"),
        (
            "Fahrzeug",
            "Kontrollschild FR 4821 (immatrikuliert)",
            "Fahrzeugausweis",
        ),
        (
            "Zustelladresse",
            "Bahnhofstrasse 12, 3011 Bern (Zustelladresse)",
            "siehe Fallakte",
        ),
    )
    for feld, angabe, bemerkung in zeilen:
        zelle = tabelle.add_row().cells
        zelle[0].text = feld
        zelle[1].text = angabe
        zelle[2].text = bemerkung

    erwartet = (
        Erwartung(_KAPO_FR, "ORGANIZATION"),
        Erwartung("Sandra Zbinden", "PERSON"),
        Erwartung("sandra.zbinden@example.ch", "EMAIL_ADDRESS"),
        Erwartung("Michael Steiner", "PERSON"),
        Erwartung("044 123 45 67", "CH_PHONE"),
        Erwartung("Zürich", "LOCATION"),
        Erwartung("FR 4821", "CH_PLATE"),
        Erwartung("3011", "CH_POSTAL_CODE"),
        Erwartung("Bahnhofstrasse", "LOCATION"),
        Erwartung("Bern", "LOCATION"),
    )
    return Testdokument(
        name="tabelle_kopf_fusszeile",
        dateiname="tabelle_kopf_fusszeile.docx",
        beschreibung=(
            "PII in Tabellenzellen sowie in Kopf- und Fusszeile — Bereiche, "
            "die python-docx zwar exponiert, die der Adapter aber explizit "
            "beschriften muss."
        ),
        inhalt=_als_bytes(dokument),
        erwartet=erwartet,
        verboten=(),
    )


# ── Dokument 5: Fussnote und Kommentar ──────────────────────────────────────


def _baue_fussnote_kommentar() -> Testdokument:
    """Fussnote (manuell per lxml eingefügt) und Kommentar mit PII.

    Der Kommentar entsteht über ``Document.add_comment`` — das kann
    ``python-docx`` 1.2.0 nativ. Die Fussnote kann es nicht; sie wird nach
    dem Speichern per ``zipfile``/``lxml`` nachgetragen, siehe
    :func:`_fussnote_einfuegen` und den Modul-Docstring.
    """
    dokument = Document()

    zeugenabsatz = dokument.add_paragraph()
    zeugenabsatz.add_run("Der Zeuge Thomas Meyer bestätigte die Aussage.")
    _fussnotenanker_einhaengen(zeugenabsatz, fussnoten_id="2")

    kommentarabsatz = dokument.add_paragraph(
        "Die Zweitmeinung wurde eingeholt und liegt der Akte bei."
    )
    kommentarlauf = kommentarabsatz.runs[0]
    dokument.add_comment(
        kommentarlauf,
        text="Rückfrage bitte an Priska Ott, priska.ott@example.ch richten.",
        author="Revision",
        initials="RV",
    )

    roh_bytes = _als_bytes(dokument)
    inhalt = _fussnote_einfuegen(
        roh_bytes,
        fussnoten_id="2",
        text=f"Vertraulich, siehe Fallakte — AHV {AHV_NUMMER}.",
    )

    erwartet = (
        Erwartung("Thomas Meyer", "PERSON"),
        Erwartung(AHV_NUMMER, "CH_AHV"),
        Erwartung("Priska Ott", "PERSON"),
        Erwartung("priska.ott@example.ch", "EMAIL_ADDRESS"),
    )
    return Testdokument(
        name="fussnote_kommentar",
        dateiname="fussnote_kommentar.docx",
        beschreibung=(
            "PII in einer Fussnote (word/footnotes.xml, per zipfile/lxml "
            "nachgetragen, da python-docx 1.2.0 keine Fussnoten erzeugen "
            "kann) und in einem Kommentar (word/comments.xml, nativ über "
            "Document.add_comment) — beide Parts erreicht nur der "
            "generische lxml-Durchlauf des Adapters, nicht python-docx "
            "direkt."
        ),
        inhalt=inhalt,
        erwartet=erwartet,
        verboten=(),
    )


# ── Dokument 6: realistisches PTI-Protokoll ─────────────────────────────────


def _baue_pti_protokoll() -> Testdokument:
    """Sitzungsprotokoll mit erfundenen Personen und echtem PTI-Fachvokabular."""
    dokument = Document()
    dokument.add_paragraph(
        "Protokoll der Sitzung zur Datenschutz-Folgeabschätzung vom "
        "14. Januar 2026."
    )
    dokument.add_paragraph(
        "Teilnehmende waren Corinne Aebischer, Projektleiterin, erreichbar "
        "unter corinne.aebischer@example.ch, sowie Martin Zürcher als "
        f"Vertreter des {_BAKOM} und Yvonne Progin von der {_KAPO_FR}."
    )
    dokument.add_paragraph(
        f"Frau Aebischer erläuterte den Stand der Beschaffungsanalyse und "
        f"die Anforderungen aus dem {_DSG}."
    )
    dokument.add_paragraph(
        f"Herr Zürcher berichtete über die laufende Zusammenarbeit mit dem "
        f"{_FEDPOL} und der {_MELDESTELLE_GWG} im Rahmen des {_GWG}."
    )
    dokument.add_paragraph(
        f"Frau Progin verwies auf die Vorgaben des {_EJPD} sowie auf das "
        f"{_BUEPF}, die im Betriebskonzept berücksichtigt werden müssen."
    )
    dokument.add_paragraph(
        "Nächste Sitzung: 18. Februar 2026. Protokoll: Corinne Aebischer, "
        "corinne.aebischer@example.ch."
    )

    erwartet = (
        # Name und Adresse stehen je zweimal im Text (Teilnehmerliste und
        # Schlusszeile) — zwei Erwartungs-Einträge pro Vorkommen.
        Erwartung("Corinne Aebischer", "PERSON"),
        Erwartung("Corinne Aebischer", "PERSON"),
        Erwartung("corinne.aebischer@example.ch", "EMAIL_ADDRESS"),
        Erwartung("corinne.aebischer@example.ch", "EMAIL_ADDRESS"),
        Erwartung("Martin Zürcher", "PERSON"),
        Erwartung("Yvonne Progin", "PERSON"),
        # Spätere Traktanden sprechen die Teilnehmenden nur noch beim
        # Nachnamen an ("Frau Aebischer …", "Frau Progin …") — auch der
        # blosse Nachname bezeichnet weiterhin eine natürliche Person.
        Erwartung("Aebischer", "PERSON"),
        Erwartung("Progin", "PERSON"),
        Erwartung(_BAKOM, "ORGANIZATION"),
        Erwartung(_KAPO_FR, "ORGANIZATION"),
        Erwartung(_FEDPOL, "ORGANIZATION"),
        Erwartung(_MELDESTELLE_GWG, "ORGANIZATION"),
        Erwartung(_EJPD, "ORGANIZATION"),
    )
    return Testdokument(
        name="pti_protokoll",
        dateiname="pti_protokoll.docx",
        beschreibung=(
            "Realistisches Sitzungsprotokoll: erfundene Personen kombiniert "
            "mit echten Behördennamen und Erlasskürzeln aus dem "
            "PTI-Fachvokabular."
        ),
        inhalt=_als_bytes(dokument),
        erwartet=erwartet,
        verboten=(),
    )


# ── Dokument 7: reiner Text ───────────────────────────────────────────────


def _baue_reiner_text() -> Testdokument:
    """Ein UTF-8-TXT-Dokument ohne jede Struktur, mit echten Umlauten."""
    text = (
        "Aktennotiz\n"
        "Datum: 5. Mai 2026\n\n"
        "Am Schalter erschien Frau Nadja Brunner und legte ihre "
        "Identitätskarte vor. Sie ist telefonisch erreichbar unter "
        "076 555 12 34 und schriftlich unter nadja.brunner@example.com.\n\n"
        f"Die Meldung wurde an die {_KAPO_FR} weitergeleitet. Zur "
        "Fallnummer 2026-0417 wurden keine weiteren Rückfragen gestellt; "
        "die Bearbeitung erfolgte im Jahr 2026 innerhalb der "
        "vorgesehenen Frist.\n\n"
        "Kontakt für Rückfragen: Peter Wälti, Sachbearbeiter."
    )
    assert is_valid_swiss_phone("076 555 12 34"), "TXT-Telefonnummer ist ungültig"

    erwartet = (
        Erwartung("Nadja Brunner", "PERSON"),
        Erwartung("076 555 12 34", "CH_PHONE"),
        Erwartung("nadja.brunner@example.com", "EMAIL_ADDRESS"),
        Erwartung(_KAPO_FR, "ORGANIZATION"),
        Erwartung("Peter Wälti", "PERSON"),
    )
    verboten = (
        Erwartung(
            "2026", "CH_POSTAL_CODE", "Jahreszahl bzw. Teil der Fallnummer"
        ),
    )
    return Testdokument(
        name="reiner_text",
        dateiname="reiner_text.txt",
        beschreibung=(
            "Reines UTF-8-Textdokument ohne Struktur — Person, Behörde, "
            "Telefon und E-Mail als erwartete Treffer, eine Jahreszahl als "
            "Fehlalarm-Falle."
        ),
        inhalt=text.encode("utf-8"),
        erwartet=erwartet,
        verboten=verboten,
    )


# ── Dokument 8: derselbe Name mehrfach, unterschiedlich viel Kontext ────────


def _baue_wiederholte_nennung() -> Testdokument:
    """Ein Firmenname, der sechsmal vorkommt — nur einmal im vollen Satz.

    Der Anlass ist ein Fehlerbild aus dem Betrieb: In der Befundliste stand ein
    Lieferantenname als Fund, im heruntergeladenen Word-Dokument stand er
    trotzdem noch im Klartext. Kein Fehler beim Zurückschreiben — das NER-Modell
    hatte ihn nur an *einer* Stelle gemeldet, und ersetzt wird, was gemeldet
    ist.

    Dieses Dokument bildet genau das ab: derselbe Name in einem
    ausformulierten Satz (dort erkennt ihn das Modell), in einer Stichwortzeile
    und in Tabellenzellen (dort nicht). Ohne
    ``pipeline.propagate_findings`` werden fünf der sechs Vorkommen verfehlt —
    dieses Dokument ist der Wächter über diesen Schritt.

    ``Talblick`` ist frei erfunden und bewusst so gewählt, dass das Modell ihn
    im Fliesstext als Organisation erkennt, kontextlos aber übersieht.
    """
    dokument = Document()

    dokument.add_paragraph(
        f"Jedes Korps hat eigene Verträge resp. Auftragsverhältnisse mit "
        f"{_LIEFERANT}. Gleichzeitig hat auch der Verein vereinzelt "
        f"Vereinbarungen mit {_LIEFERANT} abgeschlossen."
    )
    dokument.add_paragraph(
        f"Die Firma {_LIEFERANT} gibt zwar im Zusammenspiel mit dem Verein "
        f"einen Releasetakt vor."
    )
    dokument.add_paragraph(f"Vertragspartner: {_LIEFERANT}")

    tabelle = dokument.add_table(rows=2, cols=2)
    tabelle.style = "Table Grid"
    tabelle.rows[0].cells[0].text = "Lieferant"
    tabelle.rows[0].cells[1].text = _LIEFERANT
    tabelle.rows[1].cells[0].text = "Wartung"
    tabelle.rows[1].cells[1].text = _LIEFERANT

    # Sechs Vorkommen, sechs Erwartungen: der Abgleich in
    # scripts/golden_set_report.py ist eine Multimenge, jedes Vorkommen zaehlt
    # einzeln. Genau das ist hier der Punkt — eines zu finden reicht nicht.
    erwartet = tuple(Erwartung(_LIEFERANT, "ORGANIZATION") for _ in range(6))
    return Testdokument(
        name="wiederholte_nennung",
        dateiname="wiederholte_nennung.docx",
        beschreibung=(
            "Derselbe Lieferantenname sechsmal — einmal im ausformulierten "
            "Satz, sonst kontextlos in Stichwortzeile und Tabelle. Misst, ob "
            "ein Fund im ganzen Dokument gilt oder nur an seiner Fundstelle."
        ),
        inhalt=_als_bytes(dokument),
        erwartet=erwartet,
        verboten=(),
    )


# ── Modul-Konstante ─────────────────────────────────────────────────────────

def _baue_begriffsliste() -> Testdokument:
    """Prüft Ebene 0 — Begriffe, die an nichts ausser dem Listeneintrag erkennbar sind.

    Der Anlass ist ein echter Messbefund: ``Meldestelle für Geldwäscherei``
    wurde vom Sprachmodell nicht als Organisation erkannt. PTI-Behördennamen
    kennt es nicht, und Projektkürzel wie ``KEPLO`` oder ``MEF`` erst recht
    nicht — es gibt an ihnen nichts zu erkennen.

    In Produktion stehen dafür rund 670 Begriffe aus CRM, PPM und dem
    Translator-Glossar in der Liste. Dieser Testfall hält stellvertretend
    fest, dass die Ebene überhaupt greift: würde die Liste versehentlich
    geleert, liefe die Messung sonst weiterhin grün.

    Die drei Fehlalarm-Fallen prüfen die Gegenrichtung. ``MEF`` darf nicht in
    ``MEFISTO`` treffen, und ein Kürzel darf nicht auf beliebige
    Gross-/Kleinschreibung anspringen — sonst zerstört ein kurzer Eintrag
    ganze Dokumente.
    """
    projekt = "KEPLO"
    produkt = "MEF"

    text = (
        "Projektstand\n\n"
        f"Die {_MELDESTELLE_GWG} hat die Unterlagen geprüft. Zuständig ist "
        f"das {_EJPD}.\n\n"
        f"Das Projekt {projekt} liegt im Zeitplan; die Schnittstelle zum "
        f"Produkt {produkt} wird im dritten Quartal abgenommen.\n\n"
        "Nicht betroffen ist das Teilsystem MEFISTO, es gehört zu einem "
        "anderen Vorhaben.\n\n"
        "Rückfragen an Frau Corina Ammann."
    )

    begriffe = (
        {
            "term": _MELDESTELLE_GWG,
            "entity_type": "ORGANIZATION",
            "match_mode": "normalized",
        },
        {"term": _EJPD, "entity_type": "ORGANIZATION", "match_mode": "normalized"},
        {"term": projekt, "entity_type": "PTI_PROJEKT", "match_mode": "exact"},
        {"term": produkt, "entity_type": "PTI_PRODUKT", "match_mode": "exact"},
    )

    erwartet = (
        Erwartung(_MELDESTELLE_GWG, "ORGANIZATION"),
        Erwartung(_EJPD, "ORGANIZATION"),
        Erwartung(projekt, "PTI_PROJEKT"),
        Erwartung(produkt, "PTI_PRODUKT"),
        Erwartung("Corina Ammann", "PERSON"),
    )

    # Gross-/Kleinschreibung und Wortgrenzen sind bereits in
    # test_recognizer_custom_terms.py auf Einheitenebene geprueft. Hier steht
    # nur der Fall, der sich am echten Dokument zeigen muss: ein kurzer
    # Listeneintrag darf nicht mitten in einem laengeren Wort treffen.
    verboten = (
        Erwartung(
            "MEFISTO",
            "PTI_PRODUKT",
            "Der Eintrag MEF darf nicht in einem laengeren Wort treffen",
        ),
    )

    return Testdokument(
        name="begriffsliste",
        dateiname="begriffsliste.txt",
        beschreibung=(
            "Ebene 0: Behoerden-, Projekt- und Produktnamen kommen nur ueber "
            "die Begriffsliste — samt Gegenprobe auf Teilworttreffer."
        ),
        inhalt=text.encode("utf-8"),
        erwartet=erwartet,
        verboten=verboten,
        begriffe=begriffe,
    )


#: Die ausgelieferte Behoerdenliste. Erzeugt von
#: ``scripts/behoerden_aus_glossar.py`` aus dem Translator-Glossar.
_BEHOERDEN_DATEI = _REPO_ROOT / "scripts" / "data" / "behoerden.json"


def _behoerden_aus_datei() -> tuple[dict, ...]:
    """Laedt die ``sicher``-Eintraege der ausgelieferten Behoerdenliste.

    Bewusst aus der **echten Datei** und nicht aus einer Kopie im Testcode:
    Geprueft werden soll, ob die Liste, die in Produktion importiert wird,
    die Namen tatsaechlich abdeckt. Eine Kopie hier wuerde nur sich selbst
    bestaetigen und stillschweigend gruen bleiben, wenn die Aufbereitung
    einen Namen verliert.
    """
    if not _BEHOERDEN_DATEI.is_file():
        raise FileNotFoundError(
            f"{_BEHOERDEN_DATEI} fehlt. Erzeugen mit: "
            "python scripts/behoerden_aus_glossar.py"
        )
    daten = json.loads(_BEHOERDEN_DATEI.read_text(encoding="utf-8"))
    return tuple(daten["sicher"])


def _baue_behoerdenliste() -> Testdokument:
    """Prüft, ob die ausgelieferte Behördenliste im Fliesstext greift.

    Das Schwesterdokument ``begriffsliste`` prüft, dass Ebene 0 überhaupt
    wirkt — mit vier von Hand gesetzten Einträgen. Dieses hier prüft die
    andere Hälfte: ob die **konkrete Liste**, die nach Produktion geht, die
    Behördennamen enthält, an denen das Sprachmodell scheitert.

    Deshalb kommen die Begriffe aus ``scripts/data/behoerden.json`` selbst.
    Verliert die Aufbereitung einen Namen — etwa weil jemand die
    Kopfnomen-Regel in ``scripts/behoerden_aus_glossar.py`` verschärft —,
    wird dieses Dokument rot, statt dass es im Betrieb auffällt.

    Die drei ``verboten``-Einträge sind die Gegenprobe zur Regel und wiegen
    schwerer als die Treffer: ``Polizeigesetz``, ``Service Level Agreement``
    und ``Polizei-Abfrageplattform`` tragen alle ein Behördenwort im Namen
    und wurden trotzdem verworfen, weil ihr Kopfnomen ein Gesetz, einen
    Fachbegriff und eine Plattform benennt. Stünde eines davon in der Liste,
    erschiene es fortan in jedem Dokument als ``[[ORGANIZATION_n]]`` — das
    Dokument wäre verfälscht, nicht geschützt. Genau dieser Fehler war in
    einer früheren Fassung der Aufbereitung enthalten.
    """
    behoerde_bund = "Bundesamt für Polizei"
    behoerde_kanton = "Kantonspolizei Freiburg"
    behoerde_meldestelle = "Meldestelle für Geldwäscherei"

    text = (
        "Aktennotiz Rechtshilfe\n\n"
        f"Das {behoerde_bund} hat die Anfrage an die {behoerde_kanton} "
        "weitergeleitet. Eine Kopie ging an die "
        f"{behoerde_meldestelle}.\n\n"
        "Rechtsgrundlage ist das Polizeigesetz; die Fristen richten sich "
        "nach dem dort geregelten Verfahren.\n\n"
        "Der Zugriff erfolgt über die Polizei-Abfrageplattform. Für den "
        "Betrieb gilt das vereinbarte Service Level Agreement.\n\n"
        "Sachbearbeitung: Frau Beatrice Loosli."
    )

    erwartet = (
        Erwartung(behoerde_bund, "ORGANIZATION"),
        Erwartung(behoerde_kanton, "ORGANIZATION"),
        Erwartung(behoerde_meldestelle, "ORGANIZATION"),
        Erwartung("Beatrice Loosli", "PERSON"),
    )

    verboten = (
        Erwartung(
            "Polizeigesetz",
            "ORGANIZATION",
            "Kopfnomen ist ein Gesetz, keine Stelle — darf nicht in der Liste stehen",
        ),
        Erwartung(
            "Service Level Agreement",
            "ORGANIZATION",
            "Fachbegriff aus dem Glossar, keine Organisation",
        ),
        Erwartung(
            "Polizei-Abfrageplattform",
            "ORGANIZATION",
            "Kompositum mit Kopf 'Plattform' — ein System, keine Behoerde",
        ),
    )

    return Testdokument(
        name="behoerdenliste",
        dateiname="behoerdenliste.txt",
        beschreibung=(
            "Ebene 0 mit der ausgelieferten Behoerdenliste: greifen die "
            "Namen im Fliesstext, und bleiben Gesetz, Fachbegriff und "
            "System aussen vor?"
        ),
        inhalt=text.encode("utf-8"),
        erwartet=erwartet,
        verboten=verboten,
        begriffe=_behoerden_aus_datei(),
    )


DOKUMENTE: List[Testdokument] = [
    _baue_begriffsliste(),
    _baue_behoerdenliste(),
    _baue_mehrere_runs(),
    _baue_struktur_pii(),
    _baue_fehlalarm_fallen(),
    _baue_tabelle_kopf_fusszeile(),
    _baue_fussnote_kommentar(),
    _baue_pti_protokoll(),
    _baue_reiner_text(),
    _baue_wiederholte_nennung(),
]


# ── Dateien schreiben ────────────────────────────────────────────────────────


def _als_json(dokument: Testdokument) -> Dict[str, object]:
    """Baut die ``<name>.expected.json``-Struktur zu einem Testdokument."""
    return {
        "beschreibung": dokument.beschreibung,
        "erwartet": [
            {"text": e.text, "entity_type": e.entity_type}
            for e in dokument.erwartet
        ],
        "verboten": [
            {"text": v.text, "entity_type": v.entity_type, "grund": v.grund}
            for v in dokument.verboten
        ],
    }


def schreibe_dateien(ziel: Path) -> List[Path]:
    """Schreibt jedes Golden-Set-Dokument samt Erwartungsdatei nach ``ziel``.

    Parameters
    ----------
    ziel
        Zielverzeichnis (wird bei Bedarf angelegt). Pro Dokument entstehen
        zwei Dateien: ``<dateiname>`` (zum Öffnen in Word) und
        ``<name>.expected.json`` (zum Nachschlagen der Erwartungen).

    Returns
    -------
    list of Path
        Alle geschriebenen Dateien, in der Reihenfolge von ``DOKUMENTE``.
    """
    ziel.mkdir(parents=True, exist_ok=True)
    geschrieben: List[Path] = []

    for dokument in DOKUMENTE:
        dokument_pfad = ziel / dokument.dateiname
        dokument_pfad.write_bytes(dokument.inhalt)
        geschrieben.append(dokument_pfad)

        erwartung_pfad = ziel / f"{dokument.name}.expected.json"
        erwartung_pfad.write_text(
            json.dumps(_als_json(dokument), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        geschrieben.append(erwartung_pfad)

    return geschrieben


if __name__ == "__main__":
    ziel_verzeichnis = Path(__file__).resolve().parent / "dateien"
    pfade = schreibe_dateien(ziel_verzeichnis)
    print(f"{len(DOKUMENTE)} Golden-Set-Dokumente geschrieben nach {ziel_verzeichnis}:")
    for pfad in pfade:
        print(f"  {pfad}")
