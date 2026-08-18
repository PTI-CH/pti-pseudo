r"""Adapter für reine Textdateien (``text/plain``) und CSV.

Der einfachste Adapter — und trotzdem mit zwei Fallstricken, die er lösen muss:
Zeichensatz-Erkennung und Zeilenenden.

Zeichensatz
-----------
Hochgeladene Textdateien aus der Schweizer Verwaltungspraxis sind selten sauber
UTF-8. Excel-Exporte auf Windows liefern regelmässig ``cp1252`` (Windows-1252),
und wer „CSV UTF-8" in Excel wählt, bekommt UTF-8 **mit BOM**. Die Erkennung
läuft deshalb in drei Stufen:

1. **UTF-8 mit BOM** — eindeutig an den Bytes ``EF BB BF`` erkennbar.
2. **UTF-8 ohne BOM** — versuchsweise strikt dekodieren; UTF-8 ist so
   redundant kodiert, dass ein Fehlalarm auf echtem cp1252-Text praktisch
   ausgeschlossen ist (die Fortsetzungsbytes müssten zufällig passen).
3. **cp1252** — der Fallback. cp1252 kann *fast* jedes Byte dekodieren, ist
   also als letzte Stufe geeignet, aber niemals als Test tauglich: Es würde
   auch UTF-8 „erfolgreich" in Mojibake verwandeln. Deshalb steht es zuletzt.

Bewusst **keine** Heuristik-Bibliothek (``chardet``/``charset-normalizer``):
Deren Ratequote auf kurzen Dokumenten ist mässig, und ein falsch geratenes
Encoding verändert den Text still — das schlimmste Fehlerbild für ein Werkzeug,
das Beweismittel verarbeitet. Die drei Stufen oben sind erklärbar und
reproduzierbar.

Das erkannte Encoding wird beim Zurückschreiben **beibehalten**, inklusive BOM.
Eine cp1252-Datei bleibt cp1252 — sonst öffnet sie der Sachbearbeiter nach der
Pseudonymisierung als Zeichensalat und traut dem Werkzeug nicht mehr. Kann der
Ersatztext im Ursprungs-Encoding nicht dargestellt werden (Platzhalter sind
ASCII, das ist theoretisch), wird auf UTF-8 hochgestuft, statt Zeichen durch
``?`` zu ersetzen.

Zeilenenden
-----------
Es wird **nicht** normalisiert. ``\\r\\n`` bleibt ``\\r\\n``. Beim CSV-Adapter
gehören die Zeilenenden nicht zum Segmenttext, sondern werden in der Location
mitgeführt und beim Zusammensetzen wieder eingefügt.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from backend.adapters.base import DocumentAdapter, Replacement, Segment

__all__ = ["TextAdapter", "CsvAdapter", "detect_encoding"]

#: Reihenfolge ist Programm — siehe Modul-Docstring.
_UTF8_BOM = codecs.BOM_UTF8

#: Physische Zeilenenden, an denen der CSV-Adapter trennt — bewusst nur diese
#: drei. ``str.splitlines()`` erkennt zusätzlich Formularvorschub (``\x0c``),
#: die Kapitel-/Gruppen-Trenner ``\x1c``–``\x1e``, ``\x0b`` sowie die
#: Unicode-Zeilentrenner U+2028/U+2029 und U+0085 (NEL) — Zeichen, die in
#: einem CSV-Feld (z. B. aus eingefügtem Word-Text) durchaus vorkommen. Würde
#: dort mitgetrennt, risse das einen Datensatz mitten im Wort auseinander;
#: die Erkennung sähe „Mei" und „er;Andreas" statt „Meier;Andreas" und ein
#: Name bliebe unpseudonymisiert stehen.
_LINE_ENDING_RE = re.compile(r"\r\n|\r|\n")


def _split_physical_lines(text: str) -> List[str]:
    """Wie ``str.splitlines(keepends=True)``, aber nur für CRLF, CR und LF."""
    lines: List[str] = []
    pos = 0
    for match in _LINE_ENDING_RE.finditer(text):
        lines.append(text[pos : match.end()])
        pos = match.end()
    if pos < len(text):
        lines.append(text[pos:])
    return lines


def _split_content_and_ending(raw_line: str) -> Tuple[str, str]:
    r"""Trennt eine physische Zeile in Inhalt und Zeilenende-Suffix.

    Prüft ``"\r\n"`` vor den Einzelzeichen, damit ein CRLF nicht als „LF mit
    einem ``\r`` im Inhalt" fehlinterpretiert wird.
    """
    for ending in ("\r\n", "\r", "\n"):
        if raw_line.endswith(ending):
            return raw_line[: -len(ending)], ending
    return raw_line, ""


def detect_encoding(data: bytes) -> Tuple[str, bool]:
    """Ermittelt Zeichensatz und BOM-Verwendung einer Textdatei.

    Parameters
    ----------
    data
        Rohbytes der Datei.

    Returns
    -------
    (encoding, has_bom)
        ``encoding`` ist ein Python-Codec-Name (``"utf-8"`` oder ``"cp1252"``),
        ``has_bom`` sagt, ob dem UTF-8-Text ein BOM vorangestellt war.
        Bewusst getrennt zurückgegeben statt als ``"utf-8-sig"``: Beim
        Zurückschreiben muss das BOM genau einmal erscheinen, und
        ``"utf-8-sig"`` würde es beim Enkodieren automatisch hinzufügen — was
        beim BOM-losen Fall falsch wäre.
    """
    if data.startswith(_UTF8_BOM):
        return "utf-8", True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        # cp1252 hat undefinierte Bytes (0x81, 0x8D, 0x8F, 0x90, 0x9D). Falls
        # auch das scheitert, bleibt latin-1, das jedes Byte abbildet.
        try:
            data.decode("cp1252")
        except UnicodeDecodeError:
            return "latin-1", False
        return "cp1252", False
    return "utf-8", False


def _decode(data: bytes) -> Tuple[str, str, bool]:
    """Dekodiert die Bytes und gibt ``(text, encoding, has_bom)`` zurück."""
    encoding, has_bom = detect_encoding(data)
    body = data[len(_UTF8_BOM) :] if has_bom else data
    return body.decode(encoding), encoding, has_bom


def _encode(text: str, encoding: str, has_bom: bool) -> bytes:
    """Kodiert zurück und stuft notfalls auf UTF-8 hoch.

    Ein Platzhalter wie ``[[PERSON_1]]`` ist ASCII und passt in jedes der
    unterstützten Encodings. Sollte doch einmal ein Zeichen ausserhalb von
    cp1252 eingesetzt werden, ist Datenverlust die schlechtere Antwort als ein
    Encoding-Wechsel — also UTF-8 (dann mit BOM, falls das Original eines
    hatte, damit Excel die Datei weiterhin richtig öffnet).
    """
    try:
        payload = text.encode(encoding)
    except UnicodeEncodeError:
        return _UTF8_BOM + text.encode("utf-8") if has_bom else text.encode("utf-8")
    return _UTF8_BOM + payload if has_bom else payload


def apply_to_string(text: str, replacements: List[Replacement]) -> str:
    """Wendet Ersetzungen auf einen String an — von rechts nach links.

    Die gemeinsame Kernroutine aller offset-basierten Ersetzungen im flachen
    Fall. Rechts-nach-links, damit eine frühere Ersetzung die Offsets der
    späteren nicht verschiebt; würde man links beginnen, müsste man nach jeder
    Ersetzung alle folgenden Offsets um die Längendifferenz korrigieren.

    Raises
    ------
    ValueError
        Bei überlappenden Ersetzungen oder Offsets ausserhalb von ``text``.
    """
    if not replacements:
        return text

    ordered = sorted(replacements, key=lambda r: (r.start, r.end))
    previous_end = 0
    for repl in ordered:
        if repl.end > len(text):
            raise ValueError(
                f"Ersetzung {repl.start}–{repl.end} liegt ausserhalb des "
                f"Segments (Länge {len(text)})"
            )
        if repl.start < previous_end:
            raise ValueError(
                f"Überlappende Ersetzungen: {repl.start}–{repl.end} überschneidet "
                f"sich mit der vorangehenden, die bis {previous_end} reicht"
            )
        previous_end = repl.end

    result = text
    for repl in reversed(ordered):
        result = result[: repl.start] + repl.text + result[repl.end :]
    return result


@dataclass(frozen=True)
class TextLocation:
    r"""Location des Text-Adapters.

    ``line_index`` ist ``None``, wenn das Segment die ganze Datei ist
    (``TextAdapter``), sonst der 0-basierte Index der physischen Zeile
    (``CsvAdapter``). ``line_ending`` hält das Zeilenende dieser Zeile
    (``"\\r\\n"``, ``"\\n"`` oder ``""`` bei der letzten Zeile ohne Umbruch),
    damit beim Zusammensetzen exakt dasselbe wieder herauskommt.
    """

    line_index: int | None
    line_ending: str = ""


class TextAdapter(DocumentAdapter):
    """Adapter für ``text/plain``.

    **Ein Segment für das ganze Dokument.** Begründung: Reiner Text hat keine
    Struktur, die eine feinere Zerlegung rechtfertigen würde, und die
    Erkennung profitiert vom maximalen Kontext — spaCys NER braucht den Satz um
    einen Namen herum, um „Bern" als Ort und nicht als Nachname zu lesen. Ein
    Segment pro Zeile würde diesen Kontext an jedem Zeilenumbruch abschneiden,
    und in umgebrochenen Fliesstexten liegt ein Umbruch regelmässig mitten im
    Satz. Der Preis — die ganze Datei liegt als ein String im Speicher — ist in
    Phase 1 (Upload-Limit, keine Async-Jobs) irrelevant.
    """

    content_types = ("text/plain",)
    extensions = (".txt", ".text", ".md", ".log")

    #: ID des Ein-Segment-Falls.
    WHOLE_DOCUMENT_ID = "text/0"

    def extract_segments(self, data: bytes) -> List[Segment]:
        """Liefert genau ein Segment mit dem gesamten Dateiinhalt."""
        text, _encoding, _has_bom = _decode(data)
        if not text:
            return []
        return [
            Segment(
                id=self.WHOLE_DOCUMENT_ID,
                text=text,
                location=TextLocation(line_index=None),
            )
        ]

    def apply_replacements(
        self,
        data: bytes,
        replacements: Dict[str, List[Replacement]],
    ) -> bytes:
        """Ersetzt im Gesamttext und kodiert im erkannten Encoding zurück."""
        text, encoding, has_bom = _decode(data)
        repls = replacements.get(self.WHOLE_DOCUMENT_ID) or []
        return _encode(apply_to_string(text, repls), encoding, has_bom)

    def strip_metadata(self, data: bytes) -> bytes:
        """Textdateien haben keine Metadaten — die Bytes bleiben unverändert."""
        return data


class CsvAdapter(TextAdapter):
    r"""Adapter für CSV.

    **Ein Segment je physischer Zeile.** Begründung — hier kippt die Abwägung
    gegenüber ``text/plain``:

    * Eine CSV-Zeile ist ein Datensatz. Der Kontext, den die Erkennung braucht,
      steht *innerhalb* der Zeile; die Nachbarzeile ist ein anderer Fall und
      liefert keinen nützlichen Kontext, sondern nur Rauschen (spaCy sähe eine
      endlose Aufzählung ohne Satzstruktur).
    * Die Zeilennummer ist die Adresse, die Benutzer im Vorschau-Dialog
      erwarten: „Zeile 42, Spalte Nachname". Mit einem Ein-Segment-Ansatz
      müssten wir Zeichen-Offsets in Zeilennummern zurückrechnen.
    * Eine Ersetzung kann so niemals über eine Zeilengrenze laufen und die
      Datei strukturell zerstören.

    Bewusste Einschränkung: Es wird an **physischen** Zeilenumbrüchen
    getrennt, nicht CSV-korrekt geparst. Ein in Anführungszeichen
    eingebetteter Zeilenumbruch (``"Meier\\nGmbH"``) zerlegt den Datensatz
    daher in zwei Segmente. Der Vorteil überwiegt: Die Zerlegung ist
    verlustfrei und dialektunabhängig — ohne Wissen über Trennzeichen,
    Quote-Zeichen und Escape-Konvention wird kein Byte verschoben. Ein
    CSV-Parser müsste raten, und beim Zusammensetzen würde er die Datei
    normalisieren (Quoting neu setzen), also genau die Formaterhaltung
    verletzen, um die es hier geht.
    """

    content_types = ("text/csv", "application/csv", "text/tab-separated-values")
    extensions = (".csv", ".tsv")

    def extract_segments(self, data: bytes) -> List[Segment]:
        """Liefert ein Segment je physischer Zeile (ohne Zeilenende)."""
        text, _encoding, _has_bom = _decode(data)
        segments: List[Segment] = []
        for index, raw_line in enumerate(_split_physical_lines(text)):
            content, ending = _split_content_and_ending(raw_line)
            if not content:
                continue
            segments.append(
                Segment(
                    id=f"csv/{index}",
                    text=content,
                    location=TextLocation(line_index=index, line_ending=ending),
                )
            )
        return segments

    def apply_replacements(
        self,
        data: bytes,
        replacements: Dict[str, List[Replacement]],
    ) -> bytes:
        """Ersetzt zeilenweise und setzt die Datei mit ihren Zeilenenden zusammen."""
        text, encoding, has_bom = _decode(data)
        lines = _split_physical_lines(text)

        out: List[str] = []
        for index, raw_line in enumerate(lines):
            content, ending = _split_content_and_ending(raw_line)
            repls = replacements.get(f"csv/{index}") or []
            out.append(apply_to_string(content, repls) + ending)

        return _encode("".join(out), encoding, has_bom)
