# Asynchrone Aufträge — Umsetzungsplan

> **Für agentische Arbeiter:** ERFORDERLICHE UNTER-SKILL: `superpowers:subagent-driven-development` (empfohlen) oder `superpowers:executing-plans`, um diesen Plan Aufgabe für Aufgabe umzusetzen. Die Schritte tragen Kästchen (`- [ ]`) zur Nachverfolgung.

**Ziel:** Dateien oberhalb einer Grössenschwelle werden als Auftrag im Hintergrund verarbeitet, statt den HTTP-Request offen zu halten, bis der Balancer ihn abschneidet.

**Architektur:** Eine Datenbanktabelle ist die Warteschlange. Je uvicorn-Worker läuft ein Faden, der sich Aufträge zuteilt — die Zuteilung geschieht in der Datenbank, weil zwei Worker sonst denselben Auftrag doppelt bearbeiten. Die hochgeladene Datei liegt währenddessen AES-256-GCM-verschlüsselt in derselben Zeile und wird gelöscht, sobald der Auftrag endet.

**Technik:** FastAPI, SQLAlchemy 2.0, PostgreSQL (Produktion) und SQLite (Tests), `threading` aus der Standardbibliothek. **Keine neuen Abhängigkeiten.**

**Spec:** [docs/superpowers/specs/2026-08-25-asynchrone-auftraege-design.md](../specs/2026-08-25-asynchrone-auftraege-design.md)

## Übergreifende Vorgaben

Diese gelten für **jede** Aufgabe, auch wo sie nicht wiederholt werden:

- **Deutsch:** UI-Texte, Code-Kommentare und Commit-Messages. Bezeichner englisch, ausser wo der Baum bereits deutsch ist (`pipeline.py`, `wortarten.py`, `zweitmeinung.py` — dort deutsch weiterführen).
- **Conventional Commits mit Scope:** `feat(auftraege): Warteschlange teilt je Auftrag genau einmal zu`.
- **Jede neue Spalte gehört in `NACHZUZIEHENDE_SPALTEN` in `backend/db.py`.** `create_all()` legt fehlende Tabellen an, aber keine fehlenden Spalten. Ohne den Eintrag entsteht sie in Produktion nicht und fällt erst beim ersten Zugriff auf.
- **Keine echten Personendaten** in Tests, Fixtures oder Beispielen. Echte Umlaute verwenden, keine ae/oe/ue-Umschrift.
- **Keine Netzaufrufe in Tests.** Der Auftraggeber hat Messläufe gegen die Infomaniak AI Tools bis auf Weiteres untersagt; dieser Zyklus braucht sie ohnehin nicht.
- **Kein Klartext in Protokollen.** `logger` bekommt Zählbares, nie Dokumentinhalte, nie Dateinamen.
- **Status-Werte:** `wartend`, `laeuft`, `fertig`, `fehler`.
- **Vor jedem Push auf `main` und jedem Deploy beim Nutzer nachfragen.**
- **Nach jeder Aufgabe grün:** `.venv/Scripts/python -m pytest backend/tests -q` und `.venv/Scripts/python -m ruff check .`

## Dateien im Überblick

| Datei | Verantwortung |
|---|---|
| `backend/vault/crypto.py` *(geändert)* | `seal_bytes()` / `open_bytes()` neben den bestehenden `str`-Varianten |
| `backend/vault/models.py` *(geändert)* | `Job` um die neuen Spalten erweitern |
| `backend/db.py` *(geändert)* | die neuen Spalten in `NACHZUZIEHENDE_SPALTEN` |
| `backend/jobs/warteschlange.py` *(neu)* | reine Datenbanklogik: anlegen, zuteilen, abschliessen, scheitern, freigeben, aufräumen. **Kein HTTP, kein Faden.** |
| `backend/jobs/arbeiter.py` *(neu)* | der Hintergrundfaden: Schleife, Abbruch, Anbindung an die Pipeline |
| `backend/routers/jobs.py` *(neu)* | `GET /jobs/{id}` und `GET /jobs/{id}/result` |
| `backend/routers/documents.py` *(geändert)* | Schwellenprüfung in den beiden Dateirouten |
| `backend/main.py` *(geändert)* | Router einhängen, Faden im Lifespan starten und beenden |
| `frontend/src/core/api.ts` *(geändert)* | 202 erkennen, Status abfragen, Ergebnis holen |
| `frontend/src/panels/Pseudonymisieren.tsx` *(geändert)* | Wartezustand |

Die Trennung zwischen `warteschlange.py` (was mit der Datenbank geschieht) und `arbeiter.py` (wann es geschieht) ist der Kern: Die Warteschlange ist ohne Fäden testbar, und genau dort sitzen die Fehler, die weh tun.

---

## Aufgabe 1: Bytes verschlüsseln

**Dateien:**
- Ändern: `backend/vault/crypto.py`
- Test: `backend/tests/test_crypto.py`

**Schnittstellen:**
- Erzeugt: `seal_bytes(tenant_id: str, plaintext: bytes) -> bytes` und `open_bytes(tenant_id: str, blob: bytes) -> bytes`

Die vorhandenen `seal()`/`open()` arbeiten auf `str` und kodieren mit UTF-8. Ein DOCX ist keine UTF-8-Zeichenkette. Ein Umweg über Base64 würde den Speicherbedarf um ein Drittel erhöhen — bei 25-MB-Dateien in einer Datenbankzeile ist das kein Detail.

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_bytes_ueberstehen_die_versiegelung():
    """Ein DOCX ist keine UTF-8-Zeichenkette — seal() traegt es nicht."""
    from backend.vault import crypto

    rohdaten = bytes(range(256)) * 4  # alle Bytewerte, auch ungueltiges UTF-8
    blob = crypto.seal_bytes("mandant-1", rohdaten)

    assert blob != rohdaten, "es muss verschluesselt sein"
    assert crypto.open_bytes("mandant-1", blob) == rohdaten


def test_fremder_mandant_kann_bytes_nicht_oeffnen():
    """Die Mandanten-Id geht als AAD mit ein — dieselbe Sperre wie bei seal()."""
    import pytest
    from cryptography.exceptions import InvalidTag

    from backend.vault import crypto

    blob = crypto.seal_bytes("mandant-1", b"vertraulich")
    with pytest.raises(InvalidTag):
        crypto.open_bytes("mandant-2", blob)
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_crypto.py -q -k bytes`
Erwartet: FEHLER mit `AttributeError: module 'backend.vault.crypto' has no attribute 'seal_bytes'`

- [ ] **Schritt 3: Umsetzen**

In `backend/vault/crypto.py`, direkt hinter `unseal = open`:

```python
def seal_bytes(tenant_id: str, plaintext: bytes) -> bytes:
    """Wie :func:`seal`, aber fuer Rohbytes.

    Ein DOCX oder PDF ist keine UTF-8-Zeichenkette; ``seal()`` wuerde daran
    scheitern. Der Umweg ueber Base64 haette den Speicherbedarf um ein Drittel
    erhoeht — bei einer 25-MB-Datei in einer Datenbankzeile ist das keine
    Nebensache.

    Drahtformat identisch zu :func:`seal`: ``Nonce || Ciphertext || Tag``.
    Deshalb braucht die Tabelle **eine** Spalte und keine zweite fuer die Nonce.
    """
    key = _derive(get_master_key(), str(tenant_id), PURPOSE_SEAL)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, str(tenant_id).encode("utf-8"))
    return nonce + ciphertext


def open_bytes(tenant_id: str, blob: bytes) -> bytes:
    """Gegenstueck zu :func:`seal_bytes`.

    Ein fehlschlagender GCM-Tag wirft ``InvalidTag`` und wird — wie bei
    :func:`open` — absichtlich nicht abgefangen.
    """
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise VaultSealError(
            f"Auftrags-Blob ist zu kurz: {len(blob)} Byte. Ein gueltiger Blob "
            f"hat mindestens {NONCE_BYTES + TAG_BYTES} Byte (Nonce + Tag)."
        )
    key = _derive(get_master_key(), str(tenant_id), PURPOSE_SEAL)
    return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], str(tenant_id).encode("utf-8"))
```

`__all__` um `"seal_bytes"` und `"open_bytes"` ergänzen.

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_crypto.py -q`
Erwartet: alle grün

- [ ] **Schritt 5: Committen**

```bash
git add backend/vault/crypto.py backend/tests/test_crypto.py
git commit -m "feat(vault): Rohbytes versiegeln und oeffnen"
```

---

## Aufgabe 2: Die Auftragstabelle

**Dateien:**
- Ändern: `backend/vault/models.py` (Klasse `Job`)
- Ändern: `backend/db.py` (`NACHZUZIEHENDE_SPALTEN`)
- Test: `backend/tests/test_schema_nachziehen.py` *(bestehende Datei — Test dort anhängen)*

**Schnittstellen:**
- Erzeugt: `Job` mit `eingabe_blob`, `dateiname`, `parameter_json`, `ergebnis_blob`, `fortschritt`, `gesamt`, `fehler`, `aktor`, `updated_at`, `freigaben`

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_neue_auftragsspalten_stehen_in_der_nachzugsliste():
    """Ohne Eintrag entsteht die Spalte in Produktion nicht.

    `create_all()` legt fehlende TABELLEN an, aber keine fehlenden SPALTEN.
    Der Fehler faellt dann nicht beim Deploy auf, sondern beim ersten Zugriff —
    und weil die Auftragstabelle bei jedem grossen Upload gelesen wird, waere
    das Werkzeug damit ausser Betrieb.
    """
    from backend.db import NACHZUZIEHENDE_SPALTEN
    from backend.vault.models import Job

    eingetragen = {spalte for tabelle, spalte, _ in NACHZUZIEHENDE_SPALTEN if tabelle == "jobs"}
    neu = {
        "eingabe_blob", "dateiname", "parameter_json", "ergebnis_blob",
        "fortschritt", "gesamt", "fehler", "aktor", "updated_at", "freigaben",
    }
    assert neu <= eingetragen, f"nicht nachgezogen: {sorted(neu - eingetragen)}"
    assert neu <= set(Job.__table__.columns.keys())
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_schema_nachziehen.py -q -k auftragsspalten`
Erwartet: FEHLER — `nicht nachgezogen: [...]`

- [ ] **Schritt 3: Umsetzen**

In `backend/vault/models.py`, Klasse `Job` ergänzen (die bestehenden Felder bleiben):

```python
    #: Die verschluesselte Eingabe. Wird auf ``None`` gesetzt, sobald der
    #: Auftrag endet — bei Erfolg UND bei Fehler. Ein gescheiterter Auftrag
    #: darf keine Personendaten liegenlassen.
    eingabe_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dateiname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Die Wahl des Nutzers beim Anlegen: Kategorien, Modus, KI-Haekchen,
    #: Fundauswahl, bei der Rueckfuehrung die vault_id. Enthaelt KEINE Inhalte.
    parameter_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    ergebnis_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    fortschritt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gesamt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fehler: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``benutzer:<username>`` oder ``apikey:<id>`` — siehe ``core.deps.current_actor``.
    aktor: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Wie oft dieser Auftrag schon als verwaist freigegeben wurde. Ab drei
    #: gilt er als endgueltig gescheitert — sonst wandert ein Dokument, das den
    #: Arbeiter zuverlaessig abstuerzen laesst, fuer immer im Kreis.
    freigaben: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

Importe in `models.py` ergänzen: `Integer`, `LargeBinary`, `Text` aus `sqlalchemy`.

In `backend/db.py`:

```python
NACHZUZIEHENDE_SPALTEN: tuple[tuple[str, str, str], ...] = (
    # 2026-08-19 — festes Pseudonym je Begriff (CustomTerm.replacement).
    ("custom_terms", "replacement", "VARCHAR(100)"),
    # 2026-08-25 — asynchrone Auftraege (Job).
    ("jobs", "eingabe_blob", "BYTEA"),
    ("jobs", "dateiname", "VARCHAR(255)"),
    ("jobs", "parameter_json", "JSONB"),
    ("jobs", "ergebnis_blob", "BYTEA"),
    ("jobs", "fortschritt", "INTEGER"),
    ("jobs", "gesamt", "INTEGER"),
    ("jobs", "fehler", "TEXT"),
    ("jobs", "aktor", "VARCHAR(200)"),
    ("jobs", "updated_at", "TIMESTAMPTZ"),
    ("jobs", "freigaben", "INTEGER NOT NULL DEFAULT 0"),
)
```

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests -q`
Erwartet: alle grün

- [ ] **Schritt 5: Gegenprobe**

Einen Eintrag aus `NACHZUZIEHENDE_SPALTEN` entfernen und den Test erneut laufen lassen — er **muss** rot werden. Danach zurücknehmen.

- [ ] **Schritt 6: Committen**

```bash
git add backend/vault/models.py backend/db.py backend/tests/test_schema_nachziehen.py
git commit -m "feat(auftraege): Auftragstabelle um Eingabe, Ergebnis und Fortschritt erweitert"
```

---

## Aufgabe 3: Die Warteschlange — Zuteilung ohne Doppelverarbeitung

**Dateien:**
- Anlegen: `backend/jobs/__init__.py`, `backend/jobs/warteschlange.py`
- Test: `backend/tests/test_jobs_warteschlange.py`

**Schnittstellen:**
- Verbraucht: `Job` (Aufgabe 2), `crypto.seal_bytes` (Aufgabe 1)
- Erzeugt:
  - `lege_an(session, *, tenant, aktor, mode, dateiname, daten: bytes, parameter: dict) -> Job`
  - `hole_naechsten(session) -> Job | None`
  - `melde_fertig(session, job, ergebnis: bytes) -> None`
  - `melde_fehler(session, job, meldung: str) -> None`

**Dies ist die wichtigste Aufgabe des Zyklus.** Zwei Worker greifen nach derselben Zeile; ohne saubere Zuteilung wird ein Dokument doppelt verarbeitet, und der Vault bekommt zwei Zuordnungen für denselben Namen.

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_zwei_arbeiter_bekommen_nicht_denselben_auftrag(session_factory, mandant):
    """Der Waechter dieses Zyklus.

    Zwei Sitzungen holen gleichzeitig — genau eine bekommt den Auftrag. Ohne
    das wird ein Dokument doppelt verarbeitet, und der Vault vergibt zwei
    Pseudonyme fuer denselben Namen.
    """
    from backend.jobs import warteschlange

    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()

    with session_factory() as a, session_factory() as b:
        erster = warteschlange.hole_naechsten(a)
        zweiter = warteschlange.hole_naechsten(b)

    geholt = [j for j in (erster, zweiter) if j is not None]
    assert len(geholt) == 1, "genau einer darf ihn bekommen"
    assert geholt[0].status == "laeuft"


def test_die_eingabe_liegt_verschluesselt(session_factory, mandant):
    """Der Klartext darf die Tabelle nie erreichen."""
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Andrea Beispiel wohnt in Thun",
            parameter={},
        )
        s.commit()
        assert b"Andrea" not in job.eingabe_blob
        assert b"Thun" not in job.eingabe_blob


def test_fertig_loescht_die_eingabe(session_factory, mandant):
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()
        warteschlange.melde_fertig(s, job, b"Ergebnis")
        s.commit()

        assert job.status == "fertig"
        assert job.eingabe_blob is None, "die Eingabe muss weg sein"
        assert job.ergebnis_blob is not None
        assert job.expires_at is not None


def test_fehler_loescht_die_eingabe_ebenfalls(session_factory, mandant):
    """Auch der gescheiterte Auftrag darf keine Personendaten liegenlassen."""
    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        s.commit()
        warteschlange.melde_fehler(s, job, "Format nicht unterstuetzt")
        s.commit()

        assert job.status == "fehler"
        assert job.eingabe_blob is None
        assert job.fehler == "Format nicht unterstuetzt"
```

Dazu die beiden Vorrichtungen am Kopf der Testdatei:

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.vault.models import Base, Tenant


@pytest.fixture
def session_factory(monkeypatch):
    """In-Memory-Datenbank, geteilt ueber alle Sitzungen des Tests."""
    monkeypatch.setenv("VAULT_MASTER_KEY", "0" * 64)
    from backend.config import get_settings

    get_settings.cache_clear()
    # StaticPool: alle Sitzungen teilen sich dieselbe In-Memory-Datenbank.
    # Ohne ihn bekaeme jede Verbindung ihre eigene, leere — und der Test
    # "zwei Arbeiter, ein Auftrag" pruefte nichts.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    get_settings.cache_clear()


@pytest.fixture
def mandant(session_factory):
    with session_factory() as s:
        t = Tenant(name="Testmandant")
        s.add(t)
        s.commit()
        s.refresh(t)
        return t
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_warteschlange.py -q`
Erwartet: FEHLER — `ModuleNotFoundError: No module named 'backend.jobs'`

- [ ] **Schritt 3: Umsetzen**

`backend/jobs/__init__.py` bleibt leer bis auf einen Docstring.

`backend/jobs/warteschlange.py`:

```python
"""Die Auftragstabelle als Warteschlange — reine Datenbanklogik.

Bewusst ohne Faeden und ohne HTTP: Was mit der Datenbank geschieht, ist hier
und ist ohne Nebenlaeufigkeit pruefbar. Wann es geschieht, steht in
:mod:`backend.jobs.arbeiter`.

**Die Zuteilung ist der heikle Teil.** Zwei uvicorn-Worker greifen nach
derselben Zeile. Waehlte jeder Prozess fuer sich aus, bekaemen beide denselben
Auftrag: Das Dokument wuerde doppelt verarbeitet, und der Vault vergaebe zwei
Pseudonyme fuer denselben Namen. Entschieden wird deshalb in der Datenbank.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.vault import crypto
from backend.vault.models import Job

logger = logging.getLogger("pti.jobs")

#: Wie lange ein fertiges Ergebnis abholbar bleibt.
#:
#: Vierundzwanzig Stunden decken „ich mache morgen frueh weiter" ab, ohne dass
#: sich Dokumente ueber Wochen sammeln.
AUFBEWAHRUNG = timedelta(hours=24)


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def lege_an(session: Session, *, tenant, aktor: str, mode: str,
            dateiname: str, daten: bytes, parameter: dict) -> Job:
    """Legt einen wartenden Auftrag an und versiegelt die Eingabe."""
    job = Job(
        tenant_id=tenant.id,
        mode=mode,
        status="wartend",
        aktor=aktor,
        dateiname=dateiname,
        parameter_json=parameter,
        eingabe_blob=crypto.seal_bytes(str(tenant.id), daten),
        updated_at=_jetzt(),
    )
    session.add(job)
    session.flush()
    logger.info("Auftrag angelegt: %s, %d Byte", job.mode, len(daten))
    return job


def hole_naechsten(session: Session) -> Job | None:
    """Teilt genau einen wartenden Auftrag zu — oder gibt ``None`` zurueck.

    Zwei Wege, weil die Testsuite auf SQLite laeuft und PostgreSQL in
    Produktion:

    * **PostgreSQL:** ``FOR UPDATE SKIP LOCKED`` — der zweite Worker
      ueberspringt die gesperrte Zeile, statt auf sie zu warten.
    * **SQLite:** ein bedingtes ``UPDATE ... WHERE status = 'wartend'``. Die
      Zahl der betroffenen Zeilen sagt, wer gewonnen hat. SQLite kennt
      ``SKIP LOCKED`` nicht und hat ohnehin nur einen Schreiber.
    """
    ist_postgres = session.bind.dialect.name == "postgresql"

    if ist_postgres:
        job = session.scalars(
            select(Job)
            .where(Job.status == "wartend")
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if job is None:
            return None
        job.status = "laeuft"
        job.updated_at = _jetzt()
        session.flush()
        return job

    kandidat = session.scalars(
        select(Job).where(Job.status == "wartend").order_by(Job.created_at).limit(1)
    ).first()
    if kandidat is None:
        return None
    betroffen = session.execute(
        update(Job)
        .where(Job.id == kandidat.id, Job.status == "wartend")
        .values(status="laeuft", updated_at=_jetzt())
    ).rowcount
    if betroffen != 1:
        return None
    session.flush()
    session.refresh(kandidat)
    return kandidat


def melde_fertig(session: Session, job: Job, ergebnis: bytes) -> None:
    """Schliesst den Auftrag ab — und **loescht die Eingabe**."""
    job.status = "fertig"
    job.ergebnis_blob = crypto.seal_bytes(str(job.tenant_id), ergebnis)
    job.eingabe_blob = None
    job.expires_at = _jetzt() + AUFBEWAHRUNG
    job.updated_at = _jetzt()
    session.flush()
    logger.info("Auftrag fertig: %s, Ergebnis %d Byte", job.mode, len(ergebnis))


def melde_fehler(session: Session, job: Job, meldung: str) -> None:
    """Markiert den Auftrag als gescheitert — und loescht die Eingabe trotzdem.

    Auch ein gescheiterter Auftrag darf keine Personendaten liegenlassen. Das
    ist eine eigene Zusicherung mit eigenem Test, kein Nebeneffekt.
    """
    job.status = "fehler"
    job.fehler = meldung
    job.eingabe_blob = None
    job.expires_at = _jetzt() + AUFBEWAHRUNG
    job.updated_at = _jetzt()
    session.flush()
    logger.info("Auftrag gescheitert: %s", job.mode)
```

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_warteschlange.py -q`
Erwartet: alle grün

- [ ] **Schritt 5: Gegenprobe zur Zuteilung**

In `hole_naechsten` die Bedingung `Job.status == "wartend"` im `update(...)` entfernen und den Test erneut laufen lassen — `test_zwei_arbeiter_bekommen_nicht_denselben_auftrag` **muss** rot werden. Danach zurücknehmen.

- [ ] **Schritt 6: Committen**

```bash
git add backend/jobs/ backend/tests/test_jobs_warteschlange.py
git commit -m "feat(auftraege): Warteschlange teilt je Auftrag genau einmal zu"
```

---

## Aufgabe 4: Verwaiste Aufträge und Aufräumen

**Dateien:**
- Ändern: `backend/jobs/warteschlange.py`
- Test: `backend/tests/test_jobs_warteschlange.py`

**Schnittstellen:**
- Erzeugt: `gib_verwaiste_frei(session) -> int`, `raeume_ab(session) -> int`

Ein Deploy dauert 3–6 Minuten und trifft jeden laufenden Auftrag. Ohne Gegenmassnahme bliebe er für immer auf `laeuft`.

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_verwaister_auftrag_wird_wieder_freigegeben(session_factory, mandant):
    """Ein Deploy mitten im Lauf darf den Auftrag nicht fuer immer blockieren."""
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        job.status = "laeuft"
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        s.commit()

        assert warteschlange.gib_verwaiste_frei(s) == 1
        s.commit()
        s.refresh(job)
        assert job.status == "wartend"
        assert job.freigaben == 1


def test_dreimal_verwaist_gilt_als_gescheitert(session_factory, mandant):
    """Sonst wandert ein Dokument, das den Arbeiter umbringt, ewig im Kreis."""
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        job.status = "laeuft"
        job.freigaben = 2
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        s.commit()

        warteschlange.gib_verwaiste_frei(s)
        s.commit()
        s.refresh(job)
        assert job.status == "fehler"
        assert job.eingabe_blob is None, "auch hier muss die Eingabe weg sein"


def test_abgelaufener_auftrag_verschwindet(session_factory, mandant):
    from datetime import datetime, timedelta, timezone

    from backend.jobs import warteschlange
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.docx", daten=b"Inhalt", parameter={},
        )
        warteschlange.melde_fertig(s, job, b"Ergebnis")
        job.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        s.commit()

        assert warteschlange.raeume_ab(s) == 1
        s.commit()
        assert s.get(Job, job.id) is None
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_warteschlange.py -q -k "verwaist or abgelaufen"`
Erwartet: FEHLER — `AttributeError: module ... has no attribute 'gib_verwaiste_frei'`

- [ ] **Schritt 3: Umsetzen**

In `backend/jobs/warteschlange.py` ergänzen:

```python
#: Ab wann ein laufender Auftrag als verwaist gilt.
#:
#: Grosszuegig gegenueber einem langsamen Lauf (Zyklus B rechnet mit rund zwei
#: Sekunden je gescannter Seite) und knapp genug, dass niemand ewig wartet.
VERWAIST_NACH = timedelta(minutes=10)

#: Nach so vielen Freigaben gilt ein Auftrag als endgueltig gescheitert.
MAX_FREIGABEN = 3


def gib_verwaiste_frei(session: Session) -> int:
    """Holt haengengebliebene Auftraege zurueck in die Warteschlange.

    Ein Auftrag, dessen Bearbeiter mitten im Lauf verschwand — Deploy,
    Absturz, Speichermangel —, stuende sonst fuer immer auf ``laeuft``.

    Wer die Grenze :data:`MAX_FREIGABEN` reisst, gilt als gescheitert: Ein
    Dokument, das den Arbeiter zuverlaessig umbringt, soll nicht ewig im Kreis
    wandern und dabei jedes Mal einen Worker mitnehmen.
    """
    grenze = _jetzt() - VERWAIST_NACH
    verwaiste = session.scalars(
        select(Job).where(Job.status == "laeuft", Job.updated_at < grenze)
    ).all()

    for job in verwaiste:
        job.freigaben = (job.freigaben or 0) + 1
        if job.freigaben >= MAX_FREIGABEN:
            melde_fehler(
                session, job,
                "Die Verarbeitung wurde mehrfach unterbrochen und aufgegeben. "
                "Bitte laden Sie die Datei erneut hoch.",
            )
        else:
            job.status = "wartend"
            job.updated_at = _jetzt()

    if verwaiste:
        session.flush()
        logger.info("Verwaiste Auftraege freigegeben: %d", len(verwaiste))
    return len(verwaiste)


def raeume_ab(session: Session) -> int:
    """Entfernt abgelaufene Auftraege samt Ergebnis."""
    abgelaufen = session.scalars(
        select(Job).where(Job.expires_at.is_not(None), Job.expires_at < _jetzt())
    ).all()
    for job in abgelaufen:
        session.delete(job)
    if abgelaufen:
        session.flush()
        logger.info("Abgelaufene Auftraege entfernt: %d", len(abgelaufen))
    return len(abgelaufen)
```

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_warteschlange.py -q`
Erwartet: alle grün

- [ ] **Schritt 5: Committen**

```bash
git add backend/jobs/warteschlange.py backend/tests/test_jobs_warteschlange.py
git commit -m "feat(auftraege): verwaiste Auftraege zurueckholen, abgelaufene entfernen"
```

---

## Aufgabe 5: Der Hintergrundarbeiter

**Dateien:**
- Anlegen: `backend/jobs/arbeiter.py`
- Test: `backend/tests/test_jobs_arbeiter.py`

**Schnittstellen:**
- Verbraucht: `warteschlange.hole_naechsten`, `melde_fertig`, `melde_fehler`, `gib_verwaiste_frei`, `raeume_ab`
- Erzeugt: `Arbeiter(session_factory, ausfuehren)` mit `start()`, `stopp()`, `_ein_durchgang() -> bool`

**Der Durchgang ist die testbare Einheit** — nicht die Schleife. Ein Test, der einen Faden startet und wartet, ist ein Test, der irgendwann grundlos rot wird.

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_ein_durchgang_verarbeitet_genau_einen_auftrag(session_factory, mandant):
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter

    with session_factory() as s:
        warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()

    gesehen = []

    def ausfuehren(job, daten, melde_fortschritt):
        gesehen.append((job.mode, daten))
        return b"fertiges Dokument"

    arbeiter = Arbeiter(session_factory, ausfuehren)
    assert arbeiter._ein_durchgang() is True
    assert gesehen == [("anonymize", b"Inhalt")]
    assert arbeiter._ein_durchgang() is False, "danach ist nichts mehr da"


def test_ausnahme_im_lauf_wird_zum_fehlerstatus(session_factory, mandant):
    """Ein Absturz in der Verarbeitung darf den Arbeiter nicht mitnehmen."""
    from backend.jobs import warteschlange
    from backend.jobs.arbeiter import Arbeiter
    from backend.vault.models import Job

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="akte.txt", daten=b"Inhalt", parameter={},
        )
        s.commit()
        job_id = job.id

    def platzt(job, daten, melde_fortschritt):
        raise RuntimeError("Format nicht unterstuetzt")

    Arbeiter(session_factory, platzt)._ein_durchgang()

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status == "fehler"
        assert "Format nicht unterstuetzt" in job.fehler
        assert job.eingabe_blob is None
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_arbeiter.py -q`
Erwartet: FEHLER — `ModuleNotFoundError: No module named 'backend.jobs.arbeiter'`

- [ ] **Schritt 3: Umsetzen**

`backend/jobs/arbeiter.py`:

```python
"""Der Hintergrundfaden, der die Warteschlange abarbeitet.

**Warum ein Faden und kein eigener Prozess:** Der Jelastic-Runner startet genau
einen Befehl, und der Benutzer auf dem Knoten hat kein ``sudo`` — wir koennen
dort keinen zweiten Dienst einrichten. Der Arbeiter muss also in der Anwendung
leben. Je uvicorn-Worker einer; die Zuteilung regelt die Datenbank
(:mod:`backend.jobs.warteschlange`).

**Ein Auftrag je Worker gleichzeitig.** Nicht wegen der Datenbank, sondern wegen
des Speichers: Zyklus B (OCR) misst 1.1 GB Spitze je Lauf. Die Grenze gehoert
in den Code und nicht in die Knotenkonfiguration — eine Schleife, die Speicher
frisst, soll an unserer Grenze scheitern und nicht auf der Rechnung erscheinen.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from backend.jobs import warteschlange
from backend.vault import crypto

logger = logging.getLogger("pti.jobs.arbeiter")

#: Abstand zwischen zwei Blicken in die Warteschlange.
TAKT_SEKUNDEN = 2.0

#: Nur jeder n-te Durchgang raeumt auf. Abgelaufene Auftraege haben es nicht
#: eilig, und eine zusaetzliche Abfrage alle zwei Sekunden waere Verschwendung.
AUFRAEUMEN_JEDER = 150  # rund alle fuenf Minuten


class Arbeiter:
    """Holt Auftraege und fuehrt sie aus. Ein Faden je Prozess."""

    def __init__(self, session_factory, ausfuehren: Callable) -> None:
        self._session_factory = session_factory
        #: ``(job, daten: bytes, melde_fortschritt) -> bytes``
        self._ausfuehren = ausfuehren
        self._stopp = threading.Event()
        self._faden: threading.Thread | None = None
        self._durchgaenge = 0

    # ── Der testbare Kern ────────────────────────────────────────────────
    def _ein_durchgang(self) -> bool:
        """Verarbeitet hoechstens einen Auftrag. ``True``, wenn einer lief.

        Die Schleife ist absichtlich NICHT Teil davon: Ein Test, der einen
        Faden startet und auf ihn wartet, wird irgendwann grundlos rot.
        """
        self._durchgaenge += 1
        with self._session_factory() as session:
            if self._durchgaenge % AUFRAEUMEN_JEDER == 1:
                warteschlange.gib_verwaiste_frei(session)
                warteschlange.raeume_ab(session)
                session.commit()

            job = warteschlange.hole_naechsten(session)
            if job is None:
                return False
            session.commit()

            daten = crypto.open_bytes(str(job.tenant_id), job.eingabe_blob)

            def melde_fortschritt(erledigt: int, gesamt: int) -> None:
                job.fortschritt = erledigt
                job.gesamt = gesamt
                job.updated_at = datetime.now(timezone.utc)
                session.commit()

            try:
                ergebnis = self._ausfuehren(job, daten, melde_fortschritt)
            except Exception as fehler:
                # Jede Ausnahme, nicht nur die erwarteten: Ein Absturz in der
                # Verarbeitung darf den Arbeiter nicht mitnehmen — sonst steht
                # die Warteschlange still, und niemand merkt es.
                logger.warning("Auftrag gescheitert", exc_info=True)
                warteschlange.melde_fehler(session, job, str(fehler))
            else:
                warteschlange.melde_fertig(session, job, ergebnis)
            session.commit()
            return True

    # ── Schleife und Lebenszyklus ────────────────────────────────────────
    def _schleife(self) -> None:
        while not self._stopp.is_set():
            try:
                gearbeitet = self._ein_durchgang()
            except Exception:
                # Auch ein Fehler in der Warteschlange selbst (Datenbank weg)
                # darf den Faden nicht beenden.
                logger.warning("Durchgang gescheitert", exc_info=True)
                gearbeitet = False
            if not gearbeitet:
                self._stopp.wait(TAKT_SEKUNDEN)

    def start(self) -> None:
        if self._faden is not None:
            return
        self._faden = threading.Thread(target=self._schleife, name="pti-auftraege", daemon=True)
        self._faden.start()
        logger.info("Auftragsarbeiter gestartet")

    def stopp(self, frist: float = 5.0) -> None:
        self._stopp.set()
        if self._faden is not None:
            self._faden.join(timeout=frist)
            self._faden = None
        logger.info("Auftragsarbeiter beendet")
```

**Die Verdrahtung in `main.py` folgt in Aufgabe 6**, nicht hier: Der Lifespan
braucht `fuehre_aus`, und das entsteht erst dort. Sie hier zu schreiben hiesse,
einen Import auf etwas zu legen, das es noch nicht gibt — das `try/except`
haette den Fehler verschluckt und der Arbeiter waere stumm nicht gestartet.

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_arbeiter.py -q`
Erwartet: alle grün

- [ ] **Schritt 5: Gegenprobe**

Das `try/except Exception` in `_ein_durchgang` durch `except ValueError` ersetzen — `test_ausnahme_im_lauf_wird_zum_fehlerstatus` **muss** rot werden. Danach zurücknehmen.

- [ ] **Schritt 6: Committen**

```bash
git add backend/jobs/arbeiter.py backend/tests/test_jobs_arbeiter.py
git commit -m "feat(auftraege): Hintergrundarbeiter je Worker"
```

---

## Aufgabe 6: Die Ausführung — Auftrag trifft Pipeline

**Dateien:**
- Anlegen: `backend/jobs/ausfuehrung.py`
- Ändern: `backend/main.py` (Lifespan — die Verdrahtung gehört hierher, siehe unten)
- Test: `backend/tests/test_jobs_ausfuehrung.py`

**Schnittstellen:**
- Erzeugt: `fuehre_aus(job, daten: bytes, melde_fortschritt) -> bytes`

Dies ist die Naht zwischen der Warteschlange (die nichts von Dokumenten weiss) und der Pipeline (die nichts von Aufträgen weiss). Sie liest `job.mode` und `job.parameter_json` und ruft dieselben Funktionen wie der synchrone Pfad.

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_anonymize_auftrag_liefert_ein_pseudonymisiertes_dokument(session_factory, mandant):
    """Der Auftrag muss dasselbe tun wie der synchrone Pfad."""
    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    text = "Die Auskunft erteilte Frau Sandra Zbinden am Montag.".encode("utf-8")
    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="anonymize",
            dateiname="notiz.txt", daten=text,
            parameter={"modus": "anonymous", "entities": None,
                       "ki_pruefung": False, "selected_entities": None},
        )
        s.commit()

        ergebnis = fuehre_aus(job, text, lambda a, b: None)

    assert b"Zbinden" not in ergebnis, "der Name muss ersetzt sein"
    assert b"Montag" in ergebnis, "der uebrige Text bleibt"


def test_unbekannter_modus_scheitert_verstaendlich(session_factory, mandant):
    import pytest

    from backend.jobs import warteschlange
    from backend.jobs.ausfuehrung import fuehre_aus

    with session_factory() as s:
        job = warteschlange.lege_an(
            s, tenant=mandant, aktor="benutzer:tester", mode="unfug",
            dateiname="notiz.txt", daten=b"x", parameter={},
        )
        s.commit()
        with pytest.raises(ValueError, match="unfug"):
            fuehre_aus(job, b"x", lambda a, b: None)
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_ausfuehrung.py -q`
Erwartet: FEHLER — `ModuleNotFoundError`

- [ ] **Schritt 3: Umsetzen**

`backend/jobs/ausfuehrung.py`:

```python
"""Die Naht zwischen Auftrag und Pipeline.

Die Warteschlange weiss nichts von Dokumenten, die Pipeline nichts von
Auftraegen. Hier treffen sie sich — und nur hier. Dadurch bleibt beides fuer
sich pruefbar, und ein neuer Auftragstyp (Zyklus B: OCR) beruehrt genau diese
eine Datei.
"""

from __future__ import annotations

import logging

from backend.db import get_sessionmaker
from backend.pipeline import anonymize_document, reverse_document
from backend.vault.models import Job, Tenant, Vault

logger = logging.getLogger("pti.jobs.ausfuehrung")


def fuehre_aus(job: Job, daten: bytes, melde_fortschritt) -> bytes:
    """Fuehrt einen Auftrag aus und gibt die fertigen Dokumentbytes zurueck.

    ``melde_fortschritt(erledigt, gesamt)`` wird von Formaten aufgerufen, die
    eine Seitenzahl kennen. Fuer docx und xlsx gibt es keine — dort bleibt es
    bei „laeuft", und das ist ehrlicher als ein erfundener Balken.
    """
    parameter = job.parameter_json or {}
    dateiname = job.dateiname or "dokument"

    with get_sessionmaker()() as session:
        tenant = session.get(Tenant, job.tenant_id)

        if job.mode == "reverse":
            vault = session.get(Vault, parameter["vault_id"])
            ergebnis = reverse_document(session, vault, daten, dateiname)
            session.commit()
            return ergebnis

        if job.mode == "anonymize":
            # Die Begriffsliste MUSS mit. Ohne sie liefe der Auftrag still
            # ohne Ebene 0 — dieselben Behoerdennamen, die im synchronen Pfad
            # gefunden werden, blieben hier im Klartext stehen. Der Fehler
            # waere unsichtbar: Das Ergebnis saehe pseudonymisiert aus.
            # Funktionsinterner Import mit Absicht: `documents.py` bindet ab
            # Aufgabe 8 seinerseits `backend.jobs` ein. Auf Modulebene waere
            # das ein Zirkel; documents.py macht es in die andere Richtung
            # genauso.
            from backend.routers.documents import _load_custom_terms

            begriffe = _load_custom_terms(session, tenant.id)
            vault = _hole_vault(session, tenant, parameter)
            ergebnis, _befund, _gewaehlt = anonymize_document(
                session,
                vault,
                daten,
                dateiname,
                entities=parameter.get("entities"),
                custom_terms=begriffe,
                selected=parameter.get("selected_entities"),
                ki_pruefung=bool(parameter.get("ki_pruefung")),
            )
            session.commit()
            return ergebnis

    raise ValueError(f"Unbekannter Auftragstyp: {job.mode}")
```

**Nachgeschlagene Signaturen** (`backend/pipeline.py`, Stand 2026-08-25) —
nicht raten, sie stimmen so:

```python
anonymize_document(session, vault: Vault | None, data: bytes, filename: str, *,
                   entities=None, custom_terms=None, selected=None,
                   strip_metadata=True, ki_pruefung=False)
    -> tuple[bytes, DetectionResult, list[Finding]]

reverse_document(session, vault: Vault, data: bytes, filename: str) -> bytes
```

`filename` steht **positionell** an vierter Stelle, nicht als
Schlüsselwortargument.

`_load_custom_terms(session, tenant_id)` steht in
`backend/routers/documents.py:92` und wird von dort importiert statt kopiert —
zwei Fassungen derselben Abfrage wären zwei Stellen, die auseinanderlaufen.
Der Import steht **in der Funktion**, nicht am Modulkopf: `documents.py` bindet
ab Aufgabe 8 seinerseits `backend.jobs` ein, und auf Modulebene entstünde
daraus ein Zirkel.

`_hole_vault(session, tenant, parameter)` bildet nach, was `documents.py` heute
tut: bei `parameter["modus"] == "anonymous"` gibt es **keinen** Vault (`None`),
sonst wird `vault_id` verwendet oder einer mit `vault_label` neu angelegt. Die
Fassung in `documents.py` als Vorlage nehmen.

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_ausfuehrung.py -q`
Erwartet: alle grün

- [ ] **Schritt 5: Den Arbeiter im Lifespan verdrahten**

Jetzt erst — `fuehre_aus` existiert. In `backend/main.py`, im Lifespan nach dem
Laden der Engine:

```python
    # Der Auftragsarbeiter. Je uvicorn-Worker einer; die Zuteilung regelt die
    # Datenbank, sonst bekaemen beide denselben Auftrag.
    arbeiter = None
    try:
        from backend.db import get_sessionmaker
        from backend.jobs.arbeiter import Arbeiter
        from backend.jobs.ausfuehrung import fuehre_aus

        arbeiter = Arbeiter(get_sessionmaker(), fuehre_aus)
        arbeiter.start()
    except Exception:
        # Wie beim spaCy-Modell: nicht fatal. Ohne Arbeiter bleiben Auftraege
        # liegen, aber /api/health und der synchrone Pfad antworten weiter.
        logger.warning("Auftragsarbeiter konnte nicht starten", exc_info=True)
```

und im `finally`-Teil des Lifespans, vor dem bestehenden Aufräumen:

```python
        if arbeiter is not None:
            arbeiter.stopp()
```

- [ ] **Schritt 6: Die ganze Suite laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests -q`
Erwartet: alle grün — insbesondere `test_integration_api.py`, das die
Anwendung samt Lifespan hochfährt.

- [ ] **Schritt 7: Committen**

```bash
git add backend/jobs/ausfuehrung.py backend/main.py backend/tests/test_jobs_ausfuehrung.py
git commit -m "feat(auftraege): Auftrag an die Pipeline anschliessen und Arbeiter starten"
```

---

## Aufgabe 7: Status abfragen und Ergebnis holen

**Dateien:**
- Anlegen: `backend/routers/jobs.py`
- Ändern: `backend/main.py` (`DUAL_MOUNT_ROUTERS`)
- Test: `backend/tests/test_jobs_router.py`

**Schnittstellen:**
- Erzeugt: `GET /jobs/{id}` und `GET /jobs/{id}/result`

**Die Sichtbarkeitsregel ist der heikle Teil.** Ein API-Schlüssel darf die Aufträge eines anderen Schlüssels nicht sehen und erst recht nicht die eines Menschen.

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_status_zeigt_fortschritt_ohne_inhalte(client, auftrag_von):
    job = auftrag_von("benutzer:tester")
    antwort = client.get(f"/api/v1/jobs/{job.id}")

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["status"] in {"wartend", "laeuft", "fertig", "fehler"}
    assert "eingabe_blob" not in daten
    assert "parameter_json" not in daten


def test_fremder_auftrag_ist_nicht_auffindbar(client, auftrag_von):
    """404, nicht 403 — ein 403 verriete, dass es ihn gibt."""
    job = auftrag_von("apikey:00000000-0000-0000-0000-000000000001")
    antwort = client.get(f"/api/v1/jobs/{job.id}")
    assert antwort.status_code == 404


def test_ergebnis_vor_dem_ende_gibt_409(client, auftrag_von):
    job = auftrag_von("benutzer:tester", status="laeuft")
    antwort = client.get(f"/api/v1/jobs/{job.id}/result")
    assert antwort.status_code == 409
    assert antwort.json()["detail"]["status"] == "laeuft"
```


**Vorrichtungen für diese Testdatei.** Es gibt in diesem Baum **keine
`conftest.py`** — jede Testdatei bringt ihre eigene mit. Vorlage:
`backend/tests/test_auswahl_uebernahme.py:32-55`.

```python
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "c" * 64)

    from backend.config import get_settings
    from backend.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()

    from backend.main import create_app

    init_db()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()


@pytest.fixture
def auftrag_von(client):
    """Legt einen Auftrag mit gewaehltem Aktor an und gibt ihn zurueck."""
    from backend.db import get_sessionmaker
    from backend.jobs import warteschlange
    from backend.vault.models import Tenant

    def anlegen(aktor: str, status: str = "wartend"):
        with get_sessionmaker()() as s:
            tenant = s.query(Tenant).first()
            job = warteschlange.lege_an(
                s, tenant=tenant, aktor=aktor, mode="anonymize",
                dateiname="akte.txt", daten=b"Inhalt", parameter={},
            )
            job.status = status
            s.commit()
            s.refresh(job)
            s.expunge(job)
            return job

    return anlegen
```

**Der Aktor in der Vorrichtung muss zu dem passen, den `HUB_AUTH=off` erzeugt.**
`core.deps.current_actor` liefert dort den synthetischen Entwicklerbenutzer;
den tatsächlichen Wert einmal ausgeben lassen und in die Tests eintragen,
statt `"benutzer:tester"` zu raten.

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_router.py -q`
Erwartet: FEHLER — 404 auf allen Routen (Router existiert nicht)

- [ ] **Schritt 3: Umsetzen**

`backend/routers/jobs.py`:

```python
"""Statusabfrage und Abholen fertiger Auftraege.

**Sichtbarkeit:** Ein Aufrufer sieht ausschliesslich seine eigenen Auftraege.
Geprueft wird gegen ``aktor``, nicht nur gegen ``tenant_id`` — ein
API-Schluessel darf die Auftraege eines anderen Schluessels nicht sehen und
erst recht nicht die eines Menschen in der Oberflaeche.

Ein fremder oder unbekannter Auftrag ergibt **404, nicht 403**: Ein 403
verriete, dass es ihn gibt.
"""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.core.deps import current_actor, get_session, get_tenant
from backend.vault import crypto
from backend.vault.models import Job

router = APIRouter(tags=["jobs"])


def _hole_eigenen(session: Session, request: Request, job_id: str) -> Job:
    tenant = get_tenant(session, request)
    job = session.get(Job, job_id)
    if job is None or job.tenant_id != tenant.id or job.aktor != current_actor(request):
        raise HTTPException(status_code=404, detail="Auftrag nicht gefunden.")
    return job


@router.get("/jobs/{job_id}")
def status(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Status eines Auftrags. Enthaelt **keine** Dokumentinhalte."""
    job = _hole_eigenen(session, request, job_id)
    return {
        "id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "dateiname": job.dateiname,
        "fortschritt": job.fortschritt,
        "gesamt": job.gesamt,
        "fehler": job.fehler,
        "erstellt": job.created_at.isoformat() if job.created_at else None,
        "laeuft_ab": job.expires_at.isoformat() if job.expires_at else None,
    }


@router.get("/jobs/{job_id}/result")
def ergebnis(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Das fertige Dokument, Base64-kodiert wie beim synchronen Pfad."""
    job = _hole_eigenen(session, request, job_id)
    if job.status != "fertig":
        raise HTTPException(
            status_code=409,
            detail={"status": job.status, "fehler": job.fehler,
                    "meldung": "Der Auftrag ist noch nicht fertig."},
        )
    rohdaten = crypto.open_bytes(str(job.tenant_id), job.ergebnis_blob)
    return {
        "dateiname": job.dateiname,
        "content_base64": base64.b64encode(rohdaten).decode("ascii"),
    }
```

In `backend/main.py`:

```python
from backend.routers import jobs  # zu den uebrigen Importen
DUAL_MOUNT_ROUTERS = (documents, entities, vaults, terms, jobs)
```

`jobs` gehört in die Liste, `api_keys` weiterhin **nicht** — sonst könnte ein
Schlüssel sich selbst weitere Schlüssel ausstellen.

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_router.py -q`
Erwartet: alle grün

- [ ] **Schritt 5: Gegenprobe zur Sichtbarkeit**

In `_hole_eigenen` die Bedingung `job.aktor != current_actor(request)` entfernen —
`test_fremder_auftrag_ist_nicht_auffindbar` **muss** rot werden. Danach zurücknehmen.

- [ ] **Schritt 6: Committen**

```bash
git add backend/routers/jobs.py backend/main.py backend/tests/test_jobs_router.py
git commit -m "feat(auftraege): Status und Ergebnis ueber die API, nur fuer den eigenen Aktor"
```

---

## Aufgabe 8: Die Schwelle in den Dateirouten

**Dateien:**
- Ändern: `backend/routers/documents.py` (`anonymize_file` ab Zeile 322, `reverse_file` ab Zeile 426)
- Test: `backend/tests/test_jobs_schwelle.py`

**Schnittstellen:**
- Verbraucht: `warteschlange.lege_an` (Aufgabe 3)

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```python
def test_kleine_datei_bleibt_synchron(client):
    """Unterhalb der Schwelle aendert sich nichts — das ist die Zusage."""
    antwort = client.post(
        "/api/v1/documents:anonymize-file",
        files={"file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")},
        data={"mode": "anonymous"},
    )
    assert antwort.status_code == 200
    assert "job_id" not in antwort.json()
    assert "content_base64" in antwort.json()


def test_grosse_datei_wird_ein_auftrag(client, monkeypatch):
    from backend.config import get_settings

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "10")
    get_settings.cache_clear()
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize-file",
            files={"file": ("notiz.txt", b"Frau Sandra Zbinden war da.", "text/plain")},
            data={"mode": "anonymous"},
        )
        assert antwort.status_code == 202
        assert antwort.json()["status"] == "wartend"
        assert antwort.json()["job_id"]
    finally:
        get_settings.cache_clear()


def test_die_textroute_bleibt_immer_synchron(client, monkeypatch):
    """Die haeufigste Nutzung darf keinen Umweg bekommen."""
    from backend.config import get_settings

    monkeypatch.setenv("SYNC_THRESHOLD_BYTES", "1")
    get_settings.cache_clear()
    try:
        antwort = client.post(
            "/api/v1/documents:anonymize",
            json={"text": "Frau Sandra Zbinden war da.", "mode": "anonymous"},
        )
        assert antwort.status_code == 200
    finally:
        get_settings.cache_clear()
```


**Vorrichtungen für diese Testdatei.** Es gibt in diesem Baum **keine
`conftest.py`** — jede Testdatei bringt ihre eigene mit. Vorlage:
`backend/tests/test_auswahl_uebernahme.py:32-55`.

```python
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    mp = pytest.MonkeyPatch()
    mp.setenv("HUB_AUTH", "off")
    mp.setenv("DEV_ROLE", "admin")
    mp.setenv("ENV", "development")
    mp.setenv("DATABASE_URL", "sqlite://")
    mp.setenv("VAULT_MASTER_KEY", "c" * 64)

    from backend.config import get_settings
    from backend.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()

    from backend.main import create_app

    init_db()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        mp.undo()
        get_settings.cache_clear()
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests/test_jobs_schwelle.py -q`
Erwartet: `test_grosse_datei_wird_ein_auftrag` scheitert mit 200 statt 202

- [ ] **Schritt 3: Umsetzen**

In `backend/routers/documents.py`, in `anonymize_file` direkt nach `_check_size(data)`:

```python
    # Oberhalb der Schwelle wird daraus ein Auftrag. Der Grund steht in
    # `sync_threshold_bytes`: Ein grosses Dokument sprengt den Request, bevor
    # der Balancer ihn zu Ende laufen laesst.
    #
    # Die TEXTrouten bleiben davon unberuehrt — sie sind durch `max_text_chars`
    # begrenzt und laufen in Sekundenbruchteilen. Sie einzubeziehen gaebe der
    # haeufigsten Nutzung ohne Not einen Umweg.
    einstellungen = get_settings()
    if len(data) > einstellungen.sync_threshold_bytes:
        from backend.jobs import warteschlange

        job = warteschlange.lege_an(
            session,
            tenant=get_tenant(session, request),
            aktor=current_actor(request),
            mode="anonymize",
            dateiname=file.filename or "dokument",
            daten=data,
            parameter={
                "modus": mode,
                "vault_id": vault_id,
                "vault_label": vault_label,
                "entities": parsed_entities,
                "selected_entities": parsed_selected,
                "ki_pruefung": ki_pruefung,
            },
        )
        session.commit()
        return JSONResponse(
            status_code=202,
            content={"job_id": str(job.id), "status": job.status},
        )
```

Dasselbe in `reverse_file` mit `mode="reverse"` und `parameter={"vault_id": vault_id}`.

**Importe ergänzen** — nachgesehen am 2026-08-25, `documents.py` hat heute nur
`current_username` aus `core.deps`:

```python
from fastapi.responses import JSONResponse
from backend.core.deps import current_actor, current_username, get_session, get_tenant
```

`get_settings` steht bereits im Importblock (Zeile 30).

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `.venv/Scripts/python -m pytest backend/tests -q`
Erwartet: alle grün, insbesondere die bestehenden Tests in `test_integration_api.py`

- [ ] **Schritt 5: Committen**

```bash
git add backend/routers/documents.py backend/tests/test_jobs_schwelle.py
git commit -m "feat(auftraege): Dateien oberhalb der Schwelle werden Auftraege"
```

---

## Aufgabe 9: Wartezustand in der Oberfläche

**Dateien:**
- Ändern: `frontend/src/core/api.ts`
- Ändern: `frontend/src/panels/Pseudonymisieren.tsx`
- Test: `frontend/src/core/api.test.ts` *(bestehende Testdatei; sonst neu)*

**Schnittstellen:**
- Verbraucht: `GET /jobs/{id}`, `GET /jobs/{id}/result` (Aufgabe 7)
- Erzeugt: `warteAufAuftrag(jobId, onFortschritt): Promise<Ergebnis>`

- [ ] **Schritt 1: Den scheiternden Test schreiben**

```typescript
it('fragt bis zum Ende nach und liefert dann das Ergebnis', async () => {
  const antworten = [
    { status: 'wartend', fortschritt: null, gesamt: null },
    { status: 'laeuft', fortschritt: 3, gesamt: 10 },
    { status: 'fertig', fortschritt: 10, gesamt: 10 },
  ];
  const stufen: number[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    if (String(url).endsWith('/result')) {
      return new Response(JSON.stringify({ dateiname: 'a.txt', content_base64: 'eA==' }));
    }
    return new Response(JSON.stringify(antworten.shift()));
  });

  const ergebnis = await warteAufAuftrag('abc', (f, g) => stufen.push(f ?? 0));

  expect(stufen).toContain(3);
  expect(ergebnis.dateiname).toBe('a.txt');
});

it('wirft mit der Meldung des Servers, wenn der Auftrag scheitert', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ status: 'fehler', fehler: 'Format nicht unterstützt' })),
  );
  await expect(warteAufAuftrag('abc', () => {})).rejects.toThrow('Format nicht unterstützt');
});
```

- [ ] **Schritt 2: Rot laufen lassen**

Ausführen: `cd frontend && npx vitest run src/core/api.test.ts`
Erwartet: FEHLER — `warteAufAuftrag is not defined`

- [ ] **Schritt 3: Umsetzen**

In `frontend/src/core/api.ts`:

```typescript
/** Abstand zwischen zwei Nachfragen — dieselbe Grössenordnung wie im Arbeiter. */
const AUFTRAG_TAKT_MS = 2000;

/**
 * Fragt einen Auftrag ab, bis er fertig ist, und holt dann das Ergebnis.
 *
 * Scheitert der Auftrag, wird die Meldung des Servers geworfen — sie ist für
 * Menschen geschrieben und soll unverändert in der Oberfläche stehen.
 */
export async function warteAufAuftrag(
  jobId: string,
  onFortschritt: (fortschritt: number | null, gesamt: number | null) => void,
): Promise<AnonymisierErgebnis> {
  for (;;) {
    const stand = await alsJson<AuftragsStand>(await hole(`/jobs/${jobId}`));
    onFortschritt(stand.fortschritt, stand.gesamt);

    if (stand.status === 'fertig') {
      return normalisiereAntwort(await alsJson(await hole(`/jobs/${jobId}/result`)));
    }
    if (stand.status === 'fehler') {
      throw new Error(stand.fehler ?? 'Der Auftrag ist gescheitert.');
    }
    await new Promise((r) => setTimeout(r, AUFTRAG_TAKT_MS));
  }
}
```

**Nachgeschlagene Helfer** (`frontend/src/core/api.ts`, Stand 2026-08-25):
`hole(pfad, init?)` liefert eine `Response`, `alsJson<T>(res)` liest sie aus,
`normalisiereAntwort(roh)` macht daraus ein `Ergebnis`. Der Aufruf lautet also
`alsJson(await hole('/jobs/…'))` — nicht `hole()` allein.

In `anonymisiereDatei`: Bei Status **202** in `warteAufAuftrag` übergehen statt
die Antwort direkt zu normalisieren.

In `Pseudonymisieren.tsx` ein Zustandsfeld `auftrag` ergänzen und im
Wartezustand anzeigen:

```tsx
{auftrag && (
  <Hinweis ton="info">
    Die Datei wird im Hintergrund verarbeitet
    {auftrag.gesamt ? ` — Seite ${auftrag.fortschritt} von ${auftrag.gesamt}` : ''}.
    Sie können das Fenster offen lassen; die Fundliste erscheint, sobald der
    Lauf fertig ist.
  </Hinweis>
)}
```

**Die Fundliste erscheint erst am Ende.** Ein halb gefülltes Ergebnis sähe aus,
als sei das Dokument geprüft — das ist gefährlicher als gar keines.

- [ ] **Schritt 4: Grün laufen lassen**

Ausführen: `cd frontend && npm test && npx tsc --noEmit && npm run build`
Erwartet: alle grün

- [ ] **Schritt 5: Committen**

```bash
git add frontend/src
git commit -m "feat(oberflaeche): Wartezustand fuer Auftraege"
```

---

## Aufgabe 10: Dokumentation

**Dateien:**
- Ändern: `API.md`, `CLAUDE.md`, `README.MD`, `.env.example`

Diese Etappe ist ohne die Texte nicht fertig — sie ändert eine öffentliche
Zusage.

- [ ] **Schritt 1: `API.md`** — die beiden neuen Routen, die 202-Antwort, die
      Sichtbarkeitsregel (404 statt 403) und der Satz, dass die Textrouten
      synchron bleiben.

- [ ] **Schritt 2: `CLAUDE.md`** — ein Abschnitt „Asynchrone Aufträge" mit den
      drei Dingen, die man wissen muss, bevor man daran schraubt: der Arbeiter
      lebt im Worker (kein `sudo` auf dem Knoten), die Zuteilung geschieht in
      der Datenbank (zwei Worker), und die Eingabe wird bei **jedem** Ende
      gelöscht.

- [ ] **Schritt 3: `README.MD`** — bei „Was es kann" eine Zeile zu grossen
      Dateien.

- [ ] **Schritt 4: `.env.example`** — `SYNC_THRESHOLD_BYTES` steht dort noch
      nicht als aktive Einstellung; Kommentar ergänzen, dass sie jetzt wirkt.

- [ ] **Schritt 5: Dossier-Entwurf vorlegen** — §9 des Dossiers im `landing`-Repo
      sagt heute „Dokumente werden nur flüchtig während der Verarbeitung
      gehalten". Der neue Wortlaut steht in Abschnitt 9 der Spec. **Dem
      Auftraggeber vorlegen, nicht selbst setzen** — es ist seine Aussage
      gegenüber seinen Kunden.

- [ ] **Schritt 6: Committen**

```bash
git add API.md CLAUDE.md README.MD .env.example
git commit -m "docs: asynchrone Auftraege beschrieben"
```

---

## Gesamtprüfung

```bash
.venv/Scripts/python -m pytest backend/tests -q     # inkl. Golden-Set-Gate
.venv/Scripts/python -m ruff check .                # ganzes Repo
cd frontend && npm test && npx tsc --noEmit && npm run build
```

**Die sechs Sätze aus der Spec müssen durch Tests gedeckt sein:**

1. Zwei Arbeiter, ein Auftrag, genau eine Verarbeitung *(Aufgabe 3)*
2. Die Eingabe ist nach dem Ende weg — bei Erfolg **und** Fehler *(Aufgaben 3, 4)*
3. Unterhalb der Schwelle bleibt alles wie heute *(Aufgabe 8)*
4. Verwaiste Aufträge kommen zurück, nach dreimal endgültig Fehler *(Aufgabe 4)*
5. Ein API-Schlüssel sieht fremde Aufträge nicht — 404 *(Aufgabe 7)*
6. Ein abgelaufener Auftrag ist samt Ergebnis verschwunden *(Aufgabe 4)*

**Von Hand vor dem Deploy:** Eine Datei oberhalb der Schwelle durch die
Oberfläche schicken, den Wartezustand sehen, das Ergebnis herunterladen — und in
der Datenbank prüfen, dass `eingabe_blob` danach `NULL` ist.

**Beim Deploy beachten:** Die neuen Spalten entstehen über `ensure_schema()` im
Lifespan. Nach dem ersten Start auf dem Knoten prüfen, dass sie da sind:

```bash
ssh -i ~/.ssh/jelastic_ci -p 3022 213705-10792@gate.jpc.infomaniak.com \
  'set -a; . /.jelenv; set +a; cd /home/jelastic/ROOT && \
   /opt/jelastic-python314/bin/python3.14 scripts/schema_nachziehen.py --pruefen'
```
