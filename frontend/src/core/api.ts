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

import {
  merkeAuftrag,
  vergissAuftrag,
  type Auftragsart,
} from './auftragsspeicher';

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

/**
 * Rückweg: Servermodus → Modus der Oberfläche.
 *
 * Gebraucht beim Abholen eines Auftragsergebnisses. Ein unbekannter oder
 * fehlender Wert ergibt bewusst `undefined` und wird in `Ergebnis.laufModus`
 * zu `null` — **nicht** zum Vorgabewert `umkehrbar`. „Unbekannt" und
 * „umkehrbar" sind zweierlei: Wer aus einem fehlenden Feld `umkehrbar`
 * machte, legte für einen `anonym`-Lauf beim nächsten Schritt einen Tresor
 * an, den niemand bestellt hat.
 */
const MODUS_VON_API: Record<string, Modus> = {
  reversible: 'umkehrbar',
  anonymous: 'anonym',
};

/** Ein einzelner Fund der Engine. */
export interface Befund {
  /** Stabile Kennung innerhalb einer Antwort; fehlt sie, vergeben wir eine. */
  id: string;
  /**
   * Abschnitt, in dem der Fund steht (`text`, `body/p3`, `header0/p0` …).
   *
   * Muss mitgeführt werden: Der Server identifiziert einen Fund über das Tripel
   * `(segment_id, start, end)`. Fehlt der Abschnitt in `selected_entities`,
   * passt kein einziger Fund auf die Auswahl und es wird **nichts** ersetzt.
   */
  segment_id: string;
  entity_type: string;
  /** Der im Original gefundene Klartext. */
  text: string;
  start: number;
  end: number;
  /** Konfidenz 0…1 */
  score: number;
  /** Das eingesetzte Pseudonym, z. B. [[PERSON_1]]. */
  replacement?: string;
  /**
   * `true`, wenn dieser Fund aus einer gleichlautenden Stelle übertragen wurde
   * (siehe `pipeline.propagate_findings`). Für die Ersetzung gleichwertig, in
   * der Liste aber gekennzeichnet — sonst wirkt er wie ein Doppeleintrag.
   */
  propagated?: boolean;
}

export interface Ergebnis {
  /** Verarbeiteter Text (bei Texteingabe). */
  text: string | null;
  befunde: Befund[];
  /**
   * Wie viele Funde der Server tatsächlich ersetzt hat.
   *
   * Bewusst die Server-Zahl und keine im Browser gezählte: Die Auswahl kann
   * serverseitig anders ausfallen als angehakt (Default-Policy, Kategorienwahl,
   * eine Auswahl, die auf keinen Fund passt). Eine selbst gerechnete Zahl
   * behauptete dann etwas, das nicht geschehen ist.
   */
  angewendet: number | null;
  /** Nur im Modus `umkehrbar`: Kennung des angelegten Vaults. */
  vaultId: string | null;
  /** Bei Dateiverarbeitung: Link zum Herunterladen des Ergebnisses. */
  downloadUrl: string | null;
  dateiname: string | null;
  /**
   * Ob die KI-Prüfung der Namensgrenzen tatsächlich gelaufen ist.
   *
   * `false` heisst: nicht angefordert, nicht konfiguriert, nichts zu prüfen —
   * **oder der Dienst hat nicht geantwortet**. Der letzte Fall verdient einen
   * sichtbaren Hinweis: Das Ergebnis ist dann nur lokal geprüft.
   */
  kiGeprueft: boolean;
  /**
   * Die Kategorienwahl des Laufs, `null` wenn der Server sie nicht mitliefert.
   *
   * Nur der Auftragsweg füllt die drei `lauf…`-Felder (`GET /jobs/{id}/result`
   * liefert dafür eine Obermenge der synchronen Antwort, siehe API.md). Beim
   * synchronen Aufruf weiss das Panel seine eigenen Parameter noch und
   * braucht sie nicht zurück; nach einem Seitenneuladen hat es nur noch die
   * Auftragskennung, und dann ist dies die einzige Quelle.
   *
   * `null` heisst **unbekannt**, nicht „leer" und nicht „Vorgabe": entweder
   * ein Auftrag aus der Zeit vor diesen Feldern (bis 24 h nach einem Deploy
   * real möglich) oder ein Lauf ohne ausdrückliche Wahl über die externe API.
   * Wer daraus einen Vorgabewert machte, verlöre genau die Angabe, für die
   * das Feld da ist.
   */
  laufKategorien: string[] | null;
  /** Der Modus des Laufs, `null` wenn unbekannt — siehe `laufKategorien`. */
  laufModus: Modus | null;
  /** Das KI-Häkchen des Laufs (angefordert, nicht gelaufen), `null` wenn unbekannt. */
  laufKiPruefung: boolean | null;
}

/**
 * Gibt die Blob-URL eines Ergebnisses frei, falls vorhanden.
 *
 * Eine Blob-URL (Dateiergebnis, siehe `base64ZuBlobUrl`) lebt nur im
 * Browser-Speicher und wird nie automatisch aufgeräumt. Jede Stelle, die ein
 * `Ergebnis` ersetzt oder verwirft — Erfolgspfad, Fehlerpfad, Zurücksetzen —
 * muss vorher die alte URL freigeben, sonst bleibt sie bis zum Reload der
 * Seite im Speicher hängen. Ein einziger Helfer statt dreifacher
 * Duplikation in den Panels.
 */
export function gibErgebnisFrei(e: Ergebnis | null): void {
  if (e?.downloadUrl) URL.revokeObjectURL(e.downloadUrl);
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
  /** Anzahl tatsächlich ersetzter Funde. */
  applied?: number | null;
  download_url?: string | null;
  /** Dateiantworten: Base64-codierte Bytes des Ergebnisdokuments. */
  content_base64?: string | null;
  /**
   * Sowohl beim synchronen Weg (`documents:anonymize-file`) als auch beim
   * Auftrags-Endpunkt `GET /jobs/{id}/result` — Letzterer packt seit
   * Aufgabe 9 denselben Umschlag aus und liefert deshalb dieselbe Form
   * (siehe `backend/routers/jobs.py`).
   */
  filename?: string | null;
  ki_geprueft?: boolean | null;
  /** Modus des Laufs (`reversible` | `anonymous`). */
  mode?: string | null;
  /**
   * Die **gesuchten** Kategorien — bewusst nicht `entities`, das trägt die
   * **gefundenen** Stellen. Nur im Auftragsumschlag
   * (`backend/jobs/ausfuehrung.py`), beim synchronen Pfad fehlt es.
   */
  requested_entities?: string[] | null;
  /** Ob die KI-Prüfung angefordert war — Gegenstück zu `ki_geprueft` (gelaufen). */
  ki_pruefung?: boolean | null;
}

/**
 * Base64-codierte Dateibytes des Servers in eine herunterladbare Blob-URL
 * wandeln. Der Server liefert `content_base64` statt eines `download_url`,
 * weil nie Dokument-Bytes auf dem Server abgelegt werden (siehe Modulkopf
 * von `backend/routers/documents.py`) — die Antwort trägt das Dokument
 * selbst.
 *
 * Wichtig: die entstehende URL lebt nur im Browser-Speicher. Wer sie ersetzt
 * oder das Panel zurücksetzt, muss sie per `URL.revokeObjectURL` freigeben —
 * das übernehmen die Panels, nicht dieser Client.
 *
 * Liefert `null`, wenn sich die Bytes nicht decodieren lassen. Das ist kein
 * Übereifer: `atob` wirft bei abgeschnittenem oder verfälschtem Base64 einen
 * `InvalidCharacterError`, und ungefangen risse der die ganze
 * Antwort-Normalisierung mit. Verloren wäre dann auch der pseudonymisierte
 * Text, der in derselben Antwort steht und völlig in Ordnung ist — der
 * Download ist der kleinere Verlust.
 */
function base64ZuBlobUrl(base64: string): string | null {
  try {
    const binaer = atob(base64);
    const bytes = new Uint8Array(binaer.length);
    for (let i = 0; i < binaer.length; i += 1) {
      bytes[i] = binaer.charCodeAt(i);
    }
    return URL.createObjectURL(new Blob([bytes]));
  } catch {
    return null;
  }
}

function normalisiereBefunde(roh: unknown[]): Befund[] {
  return roh.map((r, i) => {
    const o = (r ?? {}) as Record<string, unknown>;
    const score = Number(o.score ?? o.confidence ?? 0);
    const segmentId = String(o.segment_id ?? 'text');
    return {
      id: String(o.id ?? `${segmentId}-${o.entity_type ?? 'E'}-${o.start ?? i}-${i}`),
      segment_id: segmentId,
      entity_type: String(o.entity_type ?? o.type ?? 'UNBEKANNT'),
      text: String(o.text ?? o.value ?? o.original ?? ''),
      start: Number(o.start ?? 0),
      end: Number(o.end ?? 0),
      score: Number.isFinite(score) ? score : 0,
      replacement: o.replacement ? String(o.replacement) : undefined,
      propagated: o.propagated === true,
    };
  });
}

function normalisiereAntwort(roh: RohAntwort): Ergebnis {
  const befundeRoh = roh.entities ?? roh.findings ?? roh.items ?? [];
  return {
    text: roh.anonymized_text ?? roh.text ?? roh.result ?? null,
    befunde: normalisiereBefunde(Array.isArray(befundeRoh) ? befundeRoh : []),
    angewendet: typeof roh.applied === 'number' ? roh.applied : null,
    vaultId: roh.vault_id ?? roh.mapping_id ?? null,
    // Scheitert das Decodieren, bleibt der Weg über eine `download_url` offen;
    // gibt es auch die nicht, entfällt nur der Download-Knopf.
    downloadUrl:
      (roh.content_base64 ? base64ZuBlobUrl(roh.content_base64) : null) ??
      (roh.download_url ?? null),
    dateiname: roh.filename ?? null,
    kiGeprueft: roh.ki_geprueft === true,
    // Die drei folgenden bleiben `null`, wenn der Server sie nicht mitschickt
    // — und nur `null`. Ein Vorgabewert an dieser Stelle wäre eine Behauptung
    // über einen Lauf, den dieser Client nicht gesehen hat.
    laufKategorien: Array.isArray(roh.requested_entities)
      ? roh.requested_entities.map(String)
      : null,
    laufModus: (roh.mode ? MODUS_VON_API[roh.mode] : undefined) ?? null,
    laufKiPruefung: typeof roh.ki_pruefung === 'boolean' ? roh.ki_pruefung : null,
  };
}

// ─── Aufträge (asynchrone Verarbeitung grosser Dateien) ────────────────────
//
// Oberhalb einer Grössenschwelle beantwortet der Server `…-file` nicht mehr
// synchron, sondern mit 202 und einer `job_id` (siehe `_check_size`-Aufrufer
// in `backend/routers/documents.py`). Die Datei läuft dann im Hintergrund;
// die Oberfläche muss nachfragen statt auf eine Antwort zu warten, die sonst
// den Balancer-Timeout risse.

/** Status eines Auftrags, wie ihn `GET /jobs/{id}` liefert. */
interface AuftragsStand {
  status: 'wartend' | 'laeuft' | 'fertig' | 'fehler';
  fortschritt: number | null;
  gesamt: number | null;
  fehler?: string | null;
}

/** Abstand zwischen zwei Nachfragen — dieselbe Grössenordnung wie im Arbeiter. */
const AUFTRAG_TAKT_MS = 2000;

/**
 * Fragt einen Auftrag ab, bis er fertig ist, und holt dann das Ergebnis.
 *
 * Scheitert der Auftrag, wird die Meldung des Servers geworfen — sie ist für
 * Menschen geschrieben (`backend/jobs/arbeiter.py`) und muss unverändert in
 * der Oberfläche stehen, nicht in einer eigenen Formulierung neu verpackt.
 *
 * Die Schleife hat bewusst **keine eigene Obergrenze**. Der Server gibt
 * verwaiste Aufträge nach zehn Minuten frei und markiert sie nach dem
 * dritten Verwaisen endgültig als `fehler` — dieser Zweig hier greift dann
 * ganz normal, ohne dass der Client mitzählen müsste. Ein zusätzliches
 * Client-Zeitlimit hätte nur eine zweite, unabhängig zu pflegende Grenze
 * geschaffen, ohne einen Fall abzudecken, den der Server nicht ohnehin
 * beendet — und ein zu kurz gewähltes Limit hätte einen noch laufenden,
 * gesunden Auftrag vorzeitig als Fehler ausgegeben.
 */
export async function warteAufAuftrag(
  jobId: string,
  onFortschritt: (fortschritt: number | null, gesamt: number | null) => void,
): Promise<Ergebnis> {
  for (;;) {
    const stand = await alsJson<AuftragsStand>(await hole(`/api/v1/jobs/${jobId}`));
    onFortschritt(stand.fortschritt, stand.gesamt);

    if (stand.status === 'fertig') {
      return normalisiereAntwort(
        await alsJson<RohAntwort>(await hole(`/api/v1/jobs/${jobId}/result`)),
      );
    }
    if (stand.status === 'fehler') {
      throw new Error(stand.fehler ?? 'Der Auftrag ist gescheitert.');
    }
    await new Promise((r) => setTimeout(r, AUFTRAG_TAKT_MS));
  }
}

/**
 * Merkt den Auftrag, wartet ihn ab und vergisst ihn wieder.
 *
 * Der einzige Weg, auf dem eine `job_id` in dieser Anwendung verfolgt wird —
 * beide 202-Zweige unten und die Wiederaufnahme beim Seitenstart gehen hier
 * durch. Getrennte Fassungen hätten unterschiedlich vergessen, und ein nicht
 * vergessener Auftrag begrüsst den Nutzer bei jedem Start aufs Neue.
 *
 * Das Vergessen steht in `finally` und greift bei Erfolg **und** bei Fehler.
 * Es greift bewusst NICHT, wenn die Seite währenddessen neu geladen wird —
 * dann läuft kein `finally`, der Eintrag überlebt, und genau darum geht es.
 *
 * **Vergessen wird nur der eigene Auftrag, erkannt an seiner Kennung.** Es
 * gibt einen Speicherplatz, und wer daneben einen zweiten Auftrag startet,
 * überschreibt ihn: Wenn dieser hier fertig wird, steht womöglich längst ein
 * fremder Eintrag darin. Ein bedingungsloses Vergessen löschte ihn — und die
 * noch laufende Verarbeitung wäre nach einem Neuladen unauffindbar. Die
 * Kennung statt der Art, weil der zweite Auftrag auch **derselben** Art sein
 * kann (siehe `vergissAuftrag`).
 */
export async function verfolgeAuftrag(
  jobId: string,
  art: Auftragsart,
  dateiname: string,
  onFortschritt: (fortschritt: number | null, gesamt: number | null) => void,
): Promise<Ergebnis> {
  merkeAuftrag({ id: jobId, art, dateiname });
  try {
    return await warteAufAuftrag(jobId, onFortschritt);
  } finally {
    vergissAuftrag(jobId);
  }
}

// ─── Kategorien (Entitätstypen) ─────────────────────────────────────────────

/** Ein Eintrag im Server-Katalog der Entitätstypen — Grundlage der Kategorienwahl. */
export interface Kategorie {
  entity_type: string;
  label: string;
  description: string;
  default_policy: 'redact' | 'keep';
  source: 'recognizer' | 'custom_terms';
}

/**
 * Katalog aller Entitätstypen, in Anzeigereihenfolge des Servers.
 *
 * Einzige Quelle für die Kategorienwahl im Panel — das Frontend hält keine
 * eigene Kopie mehr, die auseinanderlaufen könnte (siehe `core/entities.ts`).
 */
export async function ladeKategorien(): Promise<Kategorie[]> {
  const daten = await alsJson<{ entities?: Kategorie[] }>(await hole('/api/v1/entities'));
  return Array.isArray(daten?.entities) ? daten.entities : [];
}

/**
 * Ein Fund in der Form, in der ihn der Server als Auswahl wiedererkennt.
 *
 * `segment_id` ist der Teil, auf den es ankommt: Der Server vergleicht
 * `(segment_id, start, end)`. Fehlt der Abschnitt, passt die Auswahl auf keinen
 * einzigen Fund und es wird stillschweigend **nichts** ersetzt — der Aufruf
 * meldet trotzdem Erfolg.
 *
 * Bewusst OHNE `text` und `score`: Der Server identifiziert einen Fund
 * ausschliesslich über das Tripel `(segment_id, start, end)` — beide Felder
 * wären totes Gewicht. Bei einer grossen Datei landet diese Auswahl über den
 * Auftragsweg in `parameter_json`, einer normalen, unverschlüsselten
 * JSONB-Spalte; `text` wäre dort der volle Klartext des Fundes (Name,
 * AHV-Nummer, IBAN …), lesbar in jedem Datenbankabzug. `entity_type` bleibt,
 * weil er keine Personenangabe ist, sondern nur die Kategorie.
 */
function auswahlEintrag(b: Befund) {
  return {
    segment_id: b.segment_id,
    entity_type: b.entity_type,
    start: b.start,
    end: b.end,
  };
}

/**
 * Text pseudonymisieren.
 *
 * `kategorien` wird **immer** explizit mitgeschickt (nie weggelassen) — leer
 * lassen ist beim Server ein Bedienfehler (400 „Mindestens eine Kategorie
 * wählen."), kein „alles". Eine ausdrückliche Wahl schlägt dabei auch die
 * Default-Policy: wer einen KEEP-Typ wie CH_POSTAL_CODE anhakt, bekommt ihn
 * ersetzt statt nur markiert.
 *
 * `nurBefunde` (optional): die vom Menschen geprüfte Auswahl. Wird sie
 * mitgegeben, ersetzt der Server ausschliesslich diese Stellen — so wirkt das
 * Abwählen eines Fehlalarms in der Befund-Ansicht.
 */
export async function anonymisiereText(
  text: string,
  modus: Modus,
  kategorien: string[],
  kiPruefung: boolean,
  nurBefunde?: Befund[],
  signal?: AbortSignal,
): Promise<Ergebnis> {
  const koerper: Record<string, unknown> = {
    text,
    mode: MODUS_API[modus],
    language: 'de',
    entities: kategorien,
    ki_pruefung: kiPruefung,
  };
  if (nurBefunde) {
    koerper.selected_entities = nurBefunde.map(auswahlEintrag);
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

/**
 * Datei pseudonymisieren (multipart). Unterstützt .docx, .xlsx, .pptx, .txt und .csv.
 *
 * `kategorien` geht als kommagetrennte Liste ins Formular — Multipart kennt
 * keine echten Listenfelder, der Server splittet selbst (`parse_entities`).
 * `nurBefunde` aus demselben Grund als JSON-Zeichenkette.
 *
 * Die Datei muss dabei erneut hochgeladen werden: Der Server hält bewusst keine
 * Dokument-Bytes vor (siehe Modulkopf von `backend/routers/documents.py`), es
 * gibt also nichts, worauf sich ein zweiter Aufruf beziehen könnte.
 */
/**
 * `onFortschritt` wird nur gebraucht, wenn der Server mit 202 antwortet
 * (Datei oberhalb der Schwelle, siehe `warteAufAuftrag`). Beim gewohnten
 * synchronen Weg bleibt er ungenutzt.
 */
export async function anonymisiereDatei(
  datei: File,
  modus: Modus,
  kategorien: string[],
  kiPruefung: boolean,
  nurBefunde?: Befund[],
  signal?: AbortSignal,
  onFortschritt?: (fortschritt: number | null, gesamt: number | null) => void,
): Promise<Ergebnis> {
  const daten = new FormData();
  daten.append('file', datei);
  daten.append('mode', MODUS_API[modus]);
  daten.append('entities', kategorien.join(','));
  daten.append('ki_pruefung', String(kiPruefung));
  if (nurBefunde) {
    daten.append('selected_entities', JSON.stringify(nurBefunde.map(auswahlEintrag)));
  }
  const res = await hole('/api/v1/documents:anonymize-file', {
    method: 'POST',
    body: daten,
    signal,
  });
  if (res.status === 202) {
    const { job_id } = await alsJson<{ job_id: string }>(res);
    return verfolgeAuftrag(
      job_id, 'anonymisieren', datei.name, onFortschritt ?? (() => {}),
    );
  }
  return normalisiereAntwort(await alsJson<RohAntwort>(res));
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

/**
 * Dieselbe Schwelle wie bei `anonymisiereDatei` gilt auch hier
 * (`backend/routers/documents.py:reverse_file`) — eine grosse Rückführung
 * ist genauso lang unterwegs wie eine grosse Pseudonymisierung und bekommt
 * dieselbe 202-Behandlung.
 */
export async function rueckfuehrenDatei(
  datei: File,
  vaultId: string,
  signal?: AbortSignal,
  onFortschritt?: (fortschritt: number | null, gesamt: number | null) => void,
): Promise<Ergebnis> {
  const daten = new FormData();
  daten.append('file', datei);
  daten.append('vault_id', vaultId);
  const res = await hole('/api/v1/documents:reverse-file', {
    method: 'POST',
    body: daten,
    signal,
  });
  if (res.status === 202) {
    const { job_id } = await alsJson<{ job_id: string }>(res);
    return verfolgeAuftrag(
      job_id, 'rueckfuehren', datei.name, onFortschritt ?? (() => {}),
    );
  }
  return normalisiereAntwort(await alsJson<RohAntwort>(res));
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

// ─── API-Schlüssel ──────────────────────────────────────────────────────────
//
// Verwaltung nur über den SSO-Pfad, nur für Admins (`GET/POST/DELETE
// /api/v1/api-keys`) — ein Schlüssel kann sich nicht selbst weitere
// Schlüssel ausstellen. Siehe `backend/routers/api_keys.py`.

export interface ApiSchluessel {
  id: string;
  label: string;
  keyPrefix: string;
  createdBy: string;
  createdAt: string;
  expiresAt: string | null;
  lastUsedAt: string | null;
  revokedAt: string | null;
  aktiv: boolean;
}

interface RohApiSchluessel {
  id: string;
  label: string;
  key_prefix: string;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  active: boolean;
}

function normalisiereApiSchluessel(r: RohApiSchluessel): ApiSchluessel {
  return {
    id: r.id,
    label: r.label,
    keyPrefix: r.key_prefix,
    createdBy: r.created_by,
    createdAt: r.created_at,
    expiresAt: r.expires_at,
    lastUsedAt: r.last_used_at,
    revokedAt: r.revoked_at,
    aktiv: r.active,
  };
}

/** Alle Schlüssel des Mandanten, neueste zuerst. Widerrufene bleiben sichtbar. */
export async function ladeApiSchluessel(): Promise<ApiSchluessel[]> {
  const daten = await alsJson<{ api_keys?: RohApiSchluessel[] }>(await hole('/api/v1/api-keys'));
  return Array.isArray(daten?.api_keys) ? daten.api_keys.map(normalisiereApiSchluessel) : [];
}

/**
 * Neuen Schlüssel erzeugen. Der Klartext-Token steckt nur in dieser Antwort —
 * der Server speichert ausschliesslich seinen Hash und zeigt ihn danach nie
 * wieder.
 */
export async function erzeugeApiSchluessel(
  label: string,
  expiresAt: string | null,
): Promise<{ schluessel: ApiSchluessel; token: string }> {
  const roh = await alsJson<RohApiSchluessel & { token: string }>(
    await hole('/api/v1/api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, expires_at: expiresAt }),
    }),
  );
  return { schluessel: normalisiereApiSchluessel(roh), token: roh.token };
}

/** Widerruft einen Schlüssel (Soft-Delete, `revoked_at` wird gesetzt). */
export async function widerrufeApiSchluessel(id: string): Promise<void> {
  await alsJson<unknown>(
    await hole(`/api/v1/api-keys/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  );
}

// ─── Nutzung je API-Schlüssel ───────────────────────────────────────────────
//
// Quelle: `GET /api/v1/api-keys/usage`. Zugeordnet wird serverseitig über die
// Schlüssel-ID (`usage_events.username` = `apikey:<id>`) — Bezeichnungen sind
// nicht eindeutig. Die Antwort enthält ausschliesslich Zahlen und Zeitstempel,
// nie Inhalte.

/** Ein Tag der Balkenreihe. `datum` ist ein UTC-Datum (`YYYY-MM-DD`). */
export interface NutzungTag {
  datum: string;
  aufrufe: number;
  zeichen: number;
}

/** Nutzung eines einzelnen Schlüssels im gewählten Zeitfenster. */
export interface NutzungSchluessel {
  id: string;
  label: string;
  keyPrefix: string;
  aktiv: boolean;
  revokedAt: string | null;
  expiresAt: string | null;
  aufrufe: number;
  zeichen: number;
  /** Aktion (`detect`, `anonymize`, …) → Anzahl Aufrufe. */
  nachAktion: Record<string, number>;
  /** Entitätstyp (`PERSON`, `CH_IBAN`, …) → Summe der Treffer. */
  nachKategorie: Record<string, number>;
  erstesEreignis: string | null;
  letztesEreignis: string | null;
  /** Nur Tage mit Ereignissen, aufsteigend. Lücken bleiben Lücken. */
  tage: NutzungTag[];
}

export interface ApiNutzung {
  /** Fenstergrösse in Tagen, oder `null` für den gesamten Zeitraum. */
  tage: number | null;
  seit: string | null;
  schluessel: NutzungSchluessel[];
}

interface RohNutzungTag {
  date: string;
  requests: number;
  char_count: number;
}

interface RohNutzungSchluessel {
  id: string;
  label: string;
  key_prefix: string;
  active: boolean;
  revoked_at: string | null;
  expires_at: string | null;
  requests: number;
  char_count: number;
  by_action: Record<string, number>;
  entity_counts: Record<string, number>;
  first_event_at: string | null;
  last_event_at: string | null;
  daily: RohNutzungTag[];
}

/**
 * Nutzungsauswertung aller Schlüssel des Mandanten.
 *
 * `tage = null` fragt den gesamten Zeitraum ab. Der Server erwartet dafür
 * `days=0` — ein weggelassener Parameter bedeutet dort die Vorgabe von
 * 30 Tagen, nicht „alles“.
 */
export async function ladeApiNutzung(days: number | null): Promise<ApiNutzung> {
  const pfad = `/api/v1/api-keys/usage?days=${days ?? 0}`;
  const daten = await alsJson<{
    days?: number | null;
    since?: string | null;
    keys?: RohNutzungSchluessel[];
  }>(await hole(pfad));
  return {
    tage: daten?.days ?? null,
    seit: daten?.since ?? null,
    schluessel: (Array.isArray(daten?.keys) ? daten.keys : []).map((k) => ({
      id: k.id,
      label: k.label,
      keyPrefix: k.key_prefix,
      aktiv: k.active,
      revokedAt: k.revoked_at,
      expiresAt: k.expires_at,
      aufrufe: k.requests,
      zeichen: k.char_count,
      nachAktion: k.by_action ?? {},
      nachKategorie: k.entity_counts ?? {},
      erstesEreignis: k.first_event_at,
      letztesEreignis: k.last_event_at,
      tage: (k.daily ?? []).map((t) => ({
        datum: t.date,
        aufrufe: t.requests,
        zeichen: t.char_count,
      })),
    })),
  };
}
