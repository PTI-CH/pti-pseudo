"""Verarbeitungskette: Erkennung, Ersetzung und Rueckfuehrung.

Dies ist die Naht zwischen den drei unabhaengigen Bausteinen:

    Format-Adapter  →  Erkennungs-Engine  →  Vault
    (backend.adapters)  (backend.engine)     (backend.vault)

Der Adapter liefert Segmente reinen Textes, die Engine findet darin Entitaeten,
der Vault vergibt (und merkt sich) die Pseudonyme, und der Adapter schreibt die
Ersetzungen formaterhaltend zurueck.

Zwei Regeln, die hier zentral durchgesetzt werden und nirgends sonst:

1. **Ersetzungen werden von rechts nach links angewandt.** Der Adapter erwartet
   Offsets, die sich auf den unveraenderten Segmenttext beziehen. Wuerde man von
   links beginnen, verschoebe jede Ersetzung alle folgenden Offsets.
2. **Ueberlappende Treffer sind bereits in der Engine aufgeloest**
   (``engine.merge``). Hier wird das nur noch geprueft, nicht repariert — ein
   Ueberlapp an dieser Stelle waere ein Fehler weiter oben und soll auffallen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from sqlalchemy.orm import Session

from backend.adapters import get_adapter
from backend.adapters.base import DocumentAdapter, Replacement, Segment
from backend.engine import analyzer as engine
from backend.engine import entities as ents
from backend.vault import service as vault_service
from backend.vault.models import Vault

# Ein reiner Textabschnitt bekommt diese Segment-ID, damit Text- und
# Dokumentpfad dieselbe Datenstruktur benutzen koennen.
TEXT_SEGMENT_ID = "text"


@dataclass(frozen=True)
class Finding:
    """Ein einzelner Fund, angereichert um alles, was die Oberflaeche braucht."""

    segment_id: str
    entity_type: str
    start: int
    end: int
    score: float
    text: str

    @property
    def label(self) -> str:
        """Deutsches Anzeigelabel des Entitaetstyps."""
        return ents.get_label(self.entity_type)

    @property
    def confidence(self) -> str:
        """Ampelstufe fuer die Oberflaeche: gruen | gelb | rot.

        Die Schwellen entsprechen dem Konfidenz-Modell des Fachkonzepts
        (Abschnitt 13.3) und sind bewusst hier definiert und nicht im
        Frontend — die Bewertung eines Fundes ist eine fachliche Aussage,
        keine Darstellungsfrage.
        """
        if self.score >= 0.85:
            return "gruen"
        if self.score >= 0.5:
            return "gelb"
        return "rot"

    def as_dict(self) -> dict:
        """Serialisierung fuer die REST-Antwort."""
        return {
            "segment_id": self.segment_id,
            "entity_type": self.entity_type,
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "text": self.text,
        }


@dataclass
class DetectionResult:
    """Ergebnis eines reinen Erkennungslaufs, ohne Ersetzung."""

    segments: list[Segment]
    findings: list[Finding] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        """Gesamtzahl analysierter Zeichen — Grundlage der Nutzungserfassung."""
        return sum(len(s.text) for s in self.segments)

    def counts_by_type(self) -> dict[str, int]:
        """Anzahl Funde je Entitaetstyp. Enthaelt bewusst KEINE Inhalte."""
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.entity_type] = counts.get(f.entity_type, 0) + 1
        return counts


def detect_in_segments(
    segments: Sequence[Segment],
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
) -> DetectionResult:
    """Laesst die Engine ueber alle Segmente laufen.

    Jedes Segment wird einzeln analysiert: die Offsets muessen sich auf den
    jeweiligen Segmenttext beziehen, weil der Adapter genau damit
    zurueckschreibt. Ein Zusammenkleben aller Segmente waere schneller, wuerde
    aber Satzgrenzen ueber Absatz- und Zellengrenzen hinweg erfinden und die
    NER-Qualitaet verschlechtern.
    """
    result = DetectionResult(segments=list(segments))
    for segment in segments:
        if not segment.text.strip():
            continue
        for r in engine.analyze(
            text=segment.text,
            entities=entities,
            custom_terms=custom_terms,
        ):
            result.findings.append(
                Finding(
                    segment_id=segment.id,
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    score=float(r.score),
                    text=segment.text[r.start : r.end],
                )
            )
    return result


def _select(
    findings: Sequence[Finding], selected: Optional[Sequence[dict]]
) -> list[Finding]:
    """Filtert die Funde auf die vom Menschen bestaetigte Auswahl.

    Ohne Auswahl gilt die Default-Policy je Entitaetstyp: mehrdeutige Typen wie
    Postleitzahl und Kontrollschild stehen bewusst auf ``keep`` und werden nicht
    ungefragt ersetzt (sonst zerstoert ein Fehlalarm den Text).
    """
    if selected is None:
        return [
            f
            for f in findings
            if ents.get_policy(f.entity_type) is not ents.Policy.KEEP
        ]

    wanted = {(s.get("segment_id"), s.get("start"), s.get("end")) for s in selected}
    return [f for f in findings if (f.segment_id, f.start, f.end) in wanted]


def build_replacements(
    session: Session,
    vault: Optional[Vault],
    findings: Sequence[Finding],
) -> tuple[dict[str, list[Replacement]], dict[str, str]]:
    """Vergibt Pseudonyme und baut die Ersetzungsliste je Segment.

    Mit ``vault`` ist der Vorgang umkehrbar: der Vault merkt sich die Zuordnung
    verschluesselt, und derselbe Klartext bekommt stabil dasselbe Pseudonym —
    auch ueber Segmentgrenzen und ueber mehrere Dokumente hinweg.

    Ohne ``vault`` (anonymer Modus) wird nur der Typ eingesetzt und nichts
    gespeichert. Das ist endgueltig und nicht rueckfuehrbar.
    """
    per_segment: dict[str, list[Replacement]] = {}
    mapping: dict[str, str] = {}

    for f in findings:
        if vault is not None:
            pseudonym = vault_service.get_or_create_pseudonym(
                session, vault, f.entity_type, f.text
            )
        else:
            pseudonym = f"[[{f.entity_type}]]"
        mapping[f.text] = pseudonym
        per_segment.setdefault(f.segment_id, []).append(
            Replacement(start=f.start, end=f.end, text=pseudonym)
        )

    # Rechts nach links, damit frueher angewandte Ersetzungen die Offsets der
    # spaeteren nicht verschieben. Der Adapter verlaesst sich darauf.
    for replacements in per_segment.values():
        replacements.sort(key=lambda r: r.start, reverse=True)
        _assert_no_overlap(replacements)

    return per_segment, mapping


def _assert_no_overlap(replacements: Sequence[Replacement]) -> None:
    """Bricht ab, wenn sich zwei Ersetzungen ueberlappen.

    Das darf nicht vorkommen: ``engine.merge`` loest Ueberlappungen bereits auf.
    Traete es hier auf, wuerde stillschweigend Text zerstoert — lieber laut
    scheitern als ein korruptes Dokument ausliefern.
    """
    for later, earlier in zip(replacements, replacements[1:]):
        if earlier.end > later.start:
            raise ValueError(
                "Ueberlappende Ersetzungen "
                f"[{earlier.start},{earlier.end}) und [{later.start},{later.end}) — "
                "das haette die Zusammenfuehrung in engine.merge aufloesen muessen."
            )


def anonymize_text(
    session: Session,
    vault: Optional[Vault],
    text: str,
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
    selected: Optional[Sequence[dict]] = None,
) -> tuple[str, DetectionResult, list[Finding]]:
    """Pseudonymisiert einen reinen Text. Gibt Ergebnis, Befund und Auswahl zurueck."""
    segment = Segment(id=TEXT_SEGMENT_ID, text=text, location=None)
    detection = detect_in_segments(
        [segment], entities=entities, custom_terms=custom_terms
    )
    applied = _select(detection.findings, selected)
    per_segment, _ = build_replacements(session, vault, applied)

    out = text
    for r in per_segment.get(TEXT_SEGMENT_ID, []):
        out = out[: r.start] + r.text + out[r.end :]
    return out, detection, applied


def anonymize_document(
    session: Session,
    vault: Optional[Vault],
    data: bytes,
    filename: str,
    *,
    entities: Optional[Iterable[str]] = None,
    custom_terms: Optional[Sequence] = None,
    selected: Optional[Sequence[dict]] = None,
    strip_metadata: bool = True,
) -> tuple[bytes, DetectionResult, list[Finding]]:
    """Pseudonymisiert ein Dokument formaterhaltend.

    ``strip_metadata`` ist standardmaessig an: Autor und Firma in den
    Dokumenteigenschaften sind Personendaten wie jede andere auch, stehen aber
    ausserhalb des Fliesstextes und wuerden von der Erkennung nie erfasst.
    """
    adapter: DocumentAdapter = get_adapter(filename)
    segments = adapter.extract_segments(data)
    detection = detect_in_segments(
        segments, entities=entities, custom_terms=custom_terms
    )
    applied = _select(detection.findings, selected)
    per_segment, _ = build_replacements(session, vault, applied)

    out = adapter.apply_replacements(data, per_segment)
    if strip_metadata:
        out = adapter.strip_metadata(out)
    return out, detection, applied


def reverse_text(session: Session, vault: Vault, text: str) -> str:
    """Fuehrt einen pseudonymisierten Text mithilfe des Vaults zurueck."""
    return vault_service.reverse(session, vault, text)


def reverse_document(
    session: Session, vault: Vault, data: bytes, filename: str
) -> bytes:
    """Fuehrt ein pseudonymisiertes Dokument formaterhaltend zurueck.

    Gegenstueck zu :func:`anonymize_document`: statt Entitaeten zu suchen,
    werden die Pseudonyme im Text durch die entschluesselten Originale ersetzt.
    Die Ersetzung selbst macht der Vault-Dienst in einem einzigen Durchlauf.
    """
    adapter: DocumentAdapter = get_adapter(filename)
    segments = adapter.extract_segments(data)

    per_segment: dict[str, list[Replacement]] = {}
    for segment in segments:
        restored = vault_service.reverse(session, vault, segment.text)
        if restored != segment.text:
            # Das ganze Segment auf einmal ersetzen. Feiner geht es nicht, weil
            # der Vault-Dienst bewusst in einem Durchlauf arbeitet und keine
            # Einzeloffsets herausgibt (das verhindert Doppelersetzungen, wenn
            # ein Klartext selbst wie ein Pseudonym aussieht).
            per_segment[segment.id] = [
                Replacement(start=0, end=len(segment.text), text=restored)
            ]

    return adapter.apply_replacements(data, per_segment)
