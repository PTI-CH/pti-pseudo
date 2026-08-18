// Verzeichnis heisst bewusst `core/` und nicht `lib/`: Presidios Root-.gitignore
// enthaelt ein nacktes `lib/`, das jedes gleichnamige Verzeichnis in jeder Tiefe
// erfasst — `frontend/src/lib/` waere unbemerkt nie eingecheckt worden. Gleiche
// Begruendung wie bei `backend/core/` (siehe CLAUDE.md).
//
// Dünner Client für die FastAPI unter /api/v1.
//
// Grundhaltung: Das Backend ist in Phase 1 noch nicht vollständig. Jeder Aufruf
// darf fehlschlagen, und jeder Fehler muss als verständlicher deutscher Satz in
// der Oberfläche landen — nie als weisse Seite.

/** Fehlerform, die FastAPI standardmässig liefert (`detail`). */
interface FastApiFehler {
  detail?: string | { msg?: string }[];
  message?: string;
}

/** Aus einer beliebigen Fehlerantwort einen lesbaren deutschen Satz machen. */
function fehlerText(status: number, daten: unknown): string {
  const f = daten as FastApiFehler | null;
  if (f) {
    if (typeof f.detail === 'string' && f.detail) return f.detail;
    if (Array.isArray(f.detail) && f.detail[0]?.msg) return String(f.detail[0].msg);
    if (typeof f.message === 'string' && f.message) return f.message;
  }
  if (status === 401 || status === 403) {
    return 'Kein Zugriff. Bitte melden Sie sich im TIP-Hub an und öffnen Sie das Werkzeug erneut.';
  }
  if (status === 404) return 'Diese Funktion ist auf dem Server nicht verfügbar (HTTP 404).';
  if (status === 413) return 'Die Datei ist zu gross für den Server (HTTP 413).';
  if (status >= 500) {
    return `Der Server hat einen Fehler gemeldet (HTTP ${status}). Bitte versuchen Sie es später erneut.`;
  }
  return `Die Anfrage ist fehlgeschlagen (HTTP ${status}).`;
}

async function alsJson<T>(res: Response): Promise<T> {
  const text = await res.text();
  let daten: unknown = null;
  try {
    daten = text ? JSON.parse(text) : null;
  } catch {
    // Kein JSON — etwa eine HTML-Fehlerseite des vorgelagerten Proxy.
    daten = null;
  }
  if (!res.ok) throw new Error(fehlerText(res.status, daten));
  return daten as T;
}

/**
 * Netzwerkfehler (Server aus, Proxy tot) werfen ein nacktes
 * `TypeError: Failed to fetch`. Das übersetzen wir hier einmal zentral.
 */
async function hole(pfad: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(pfad, init);
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new Error('Der Server ist nicht erreichbar. Läuft das Backend auf Port 8000?');
  }
}

/** Fehlerobjekt beliebiger Herkunft in einen Anzeigetext verwandeln. */
export function fehlerMeldung(e: unknown): string {
  if (e instanceof Error && e.message) return e.message;
  return 'Unbekannter Fehler.';
}

// ─── Pseudonymisieren ───────────────────────────────────────────────────────

/** `umkehrbar` legt einen Vault an, `anonym` verwirft die Zuordnung sofort. */
export type Modus = 'umkehrbar' | 'anonym';

/** Werte, wie sie das Backend erwartet. */
const MODUS_API: Record<Modus, string> = {
  umkehrbar: 'reversible',
  anonym: 'anonymous',
};

/** Ein einzelner Fund der Engine. */
export interface Befund {
  /** Stabile Kennung innerhalb einer Antwort; fehlt sie, vergeben wir eine. */
  id: string;
  entity_type: string;
  /** Der im Original gefundene Klartext. */
  text: string;
  start: number;
  end: number;
  /** Konfidenz 0…1 */
  score: number;
  /** Das eingesetzte Pseudonym, z. B. [[PERSON_1]]. */
  replacement?: string;
}

export interface Ergebnis {
  /** Verarbeiteter Text (bei Texteingabe). */
  text: string | null;
  befunde: Befund[];
  /** Nur im Modus `umkehrbar`: Kennung des angelegten Vaults. */
  vaultId: string | null;
  /** Bei Dateiverarbeitung: Link zum Herunterladen des Ergebnisses. */
  downloadUrl: string | null;
  dateiname: string | null;
}

/** Rohform, wie sie vom Server kommen darf — bewusst tolerant gelesen. */
interface RohAntwort {
  text?: string | null;
  anonymized_text?: string | null;
  result?: string | null;
  entities?: unknown[];
  findings?: unknown[];
  items?: unknown[];
  vault_id?: string | null;
  mapping_id?: string | null;
  download_url?: string | null;
  filename?: string | null;
}

function normalisiereBefunde(roh: unknown[]): Befund[] {
  return roh.map((r, i) => {
    const o = (r ?? {}) as Record<string, unknown>;
    const score = Number(o.score ?? o.confidence ?? 0);
    return {
      id: String(o.id ?? `${o.entity_type ?? 'E'}-${o.start ?? i}-${i}`),
      entity_type: String(o.entity_type ?? o.type ?? 'UNBEKANNT'),
      text: String(o.text ?? o.value ?? o.original ?? ''),
      start: Number(o.start ?? 0),
      end: Number(o.end ?? 0),
      score: Number.isFinite(score) ? score : 0,
      replacement: o.replacement ? String(o.replacement) : undefined,
    };
  });
}

function normalisiereAntwort(roh: RohAntwort): Ergebnis {
  const befundeRoh = roh.entities ?? roh.findings ?? roh.items ?? [];
  return {
    text: roh.anonymized_text ?? roh.text ?? roh.result ?? null,
    befunde: normalisiereBefunde(Array.isArray(befundeRoh) ? befundeRoh : []),
    vaultId: roh.vault_id ?? roh.mapping_id ?? null,
    downloadUrl: roh.download_url ?? null,
    dateiname: roh.filename ?? null,
  };
}

/**
 * Text pseudonymisieren.
 *
 * `nurBefunde` (optional): die vom Menschen geprüfte Auswahl. Wird sie
 * mitgegeben, ersetzt der Server ausschliesslich diese Stellen — so wirkt das
 * Abwählen eines Fehlalarms in der Befund-Ansicht.
 */
export async function anonymisiereText(
  text: string,
  modus: Modus,
  nurBefunde?: Befund[],
  signal?: AbortSignal,
): Promise<Ergebnis> {
  const koerper: Record<string, unknown> = { text, mode: MODUS_API[modus], language: 'de' };
  if (nurBefunde) {
    koerper.selected_entities = nurBefunde.map((b) => ({
      entity_type: b.entity_type,
      start: b.start,
      end: b.end,
      score: b.score,
      text: b.text,
    }));
  }
  return normalisiereAntwort(
    await alsJson<RohAntwort>(
      await hole('/api/v1/documents:anonymize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(koerper),
        signal,
      }),
    ),
  );
}

/** Datei pseudonymisieren (multipart). Unterstützt .docx, .txt und .csv. */
export async function anonymisiereDatei(
  datei: File,
  modus: Modus,
  signal?: AbortSignal,
): Promise<Ergebnis> {
  const daten = new FormData();
  daten.append('file', datei);
  daten.append('mode', MODUS_API[modus]);
  daten.append('language', 'de');
  return normalisiereAntwort(
    await alsJson<RohAntwort>(
      await hole('/api/v1/documents:anonymize', { method: 'POST', body: daten, signal }),
    ),
  );
}

// ─── Rückführen ─────────────────────────────────────────────────────────────

export async function rueckfuehrenText(
  text: string,
  vaultId: string,
  signal?: AbortSignal,
): Promise<Ergebnis> {
  return normalisiereAntwort(
    await alsJson<RohAntwort>(
      await hole('/api/v1/documents:reverse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, vault_id: vaultId }),
        signal,
      }),
    ),
  );
}

export async function rueckfuehrenDatei(
  datei: File,
  vaultId: string,
  signal?: AbortSignal,
): Promise<Ergebnis> {
  const daten = new FormData();
  daten.append('file', datei);
  daten.append('vault_id', vaultId);
  return normalisiereAntwort(
    await alsJson<RohAntwort>(
      await hole('/api/v1/documents:reverse', { method: 'POST', body: daten, signal }),
    ),
  );
}

// ─── Vaults (Zuordnungstabellen) ────────────────────────────────────────────

export interface Vault {
  id: string;
  /** Ursprungsdokument oder Kurzbeschreibung, falls der Server eine liefert. */
  bezeichnung: string | null;
  erstelltAm: string | null;
  anzahlEintraege: number | null;
}

interface RohVault {
  id?: string | number;
  vault_id?: string | number;
  name?: string | null;
  label?: string | null;
  document_name?: string | null;
  filename?: string | null;
  created_at?: string | null;
  entry_count?: number | null;
  entity_count?: number | null;
}

/** Der Server darf `{mappings: []}`, `{items: []}` oder ein nacktes Array liefern. */
function listeAus(daten: unknown, schluessel: string[]): unknown[] {
  if (Array.isArray(daten)) return daten;
  const o = (daten ?? {}) as Record<string, unknown>;
  for (const k of schluessel) {
    if (Array.isArray(o[k])) return o[k] as unknown[];
  }
  return [];
}

export async function ladeVaults(): Promise<Vault[]> {
  const daten = await alsJson<unknown>(await hole('/api/v1/mappings'));
  return listeAus(daten, ['mappings', 'items', 'results']).map((r) => {
    const v = (r ?? {}) as RohVault;
    return {
      id: String(v.id ?? v.vault_id ?? ''),
      bezeichnung: v.document_name ?? v.filename ?? v.name ?? v.label ?? null,
      erstelltAm: v.created_at ?? null,
      anzahlEintraege: v.entry_count ?? v.entity_count ?? null,
    };
  });
}

// ─── Begriffsliste ──────────────────────────────────────────────────────────

export interface Begriff {
  id: string;
  begriff: string;
  /** Typ, als der er behandelt wird — steuert die Form des Pseudonyms. */
  typ: string;
  ersatz: string | null;
}

interface RohBegriff {
  id?: string | number;
  term?: string;
  text?: string;
  value?: string;
  entity_type?: string;
  type?: string;
  replacement?: string | null;
}

export async function ladeBegriffe(): Promise<Begriff[]> {
  const daten = await alsJson<unknown>(await hole('/api/v1/terms'));
  return listeAus(daten, ['terms', 'items', 'results']).map((r, i) => {
    const b = (r ?? {}) as RohBegriff;
    return {
      id: String(b.id ?? i),
      begriff: String(b.term ?? b.text ?? b.value ?? ''),
      typ: String(b.entity_type ?? b.type ?? 'PERSON'),
      ersatz: b.replacement ?? null,
    };
  });
}

export async function legeBegriffAn(
  begriff: string,
  typ: string,
  ersatz: string | null,
): Promise<void> {
  await alsJson<unknown>(
    await hole('/api/v1/terms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ term: begriff, entity_type: typ, replacement: ersatz || null }),
    }),
  );
}

export async function loescheBegriff(id: string): Promise<void> {
  await alsJson<unknown>(
    await hole(`/api/v1/terms/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  );
}
