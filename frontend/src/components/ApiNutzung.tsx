import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import Hinweis from './Hinweis';
import StatusBadge, { schluesselStatus } from './SchluesselStatus';
import { labelFuerTyp } from '../core/entities';
import {
  fehlerMeldung,
  ladeApiNutzung,
  type ApiNutzung as NutzungDaten,
  type NutzungSchluessel,
} from '../core/api';

/**
 * Auswertbare Zeiträume. `null` heisst „gesamter Zeitraum“ — der API-Client
 * schickt dafür `days=0`, weil ein weggelassener Parameter serverseitig die
 * Vorgabe von 30 Tagen bedeutet.
 */
const ZEITRAEUME: { tage: number | null; label: string }[] = [
  { tage: 7, label: '7 Tage' },
  { tage: 30, label: '30 Tage' },
  { tage: 90, label: '90 Tage' },
  { tage: 365, label: '365 Tage' },
  { tage: null, label: 'Alles' },
];

/**
 * Deutsche Namen der Aktionen aus `usage_events.action`. Ein unbekannter Wert
 * (neue Aktion im Backend, die die Oberfläche noch nicht kennt) wird roh
 * durchgereicht statt verschluckt — gleiche Haltung wie `labelFuerTyp`.
 */
const AKTION_LABEL: Record<string, string> = {
  detect: 'Erkennen',
  anonymize: 'Pseudonymisieren (Text)',
  anonymize_file: 'Pseudonymisieren (Datei)',
  reverse: 'Rückführen (Text)',
  reverse_file: 'Rückführen (Datei)',
};

function aktionLabel(aktion: string): string {
  return AKTION_LABEL[aktion] ?? aktion;
}

function zahl(n: number): string {
  return n.toLocaleString('de-CH');
}

function datumZeitLesbar(iso: string | null): string {
  if (!iso) return 'nie';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('de-CH', { dateStyle: 'short', timeStyle: 'short' });
}

/** `YYYY-MM-DD` als `TT.MM.JJJJ` — ohne Uhrzeit, es ist ein Tageswert. */
function tagLesbar(datum: string): string {
  const d = new Date(`${datum}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return datum;
  return d.toLocaleDateString('de-CH', { timeZone: 'UTC' });
}

/** Chip für ein Zähler-Paar (Aktion oder Kategorie). */
function Chip({ text, anzahl }: { text: string; anzahl: number }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 dark:bg-gray-700 px-2.5 py-0.5 text-xs text-gray-700 dark:text-gray-200">
      {text}
      <span className="font-semibold tabular-nums text-gray-900 dark:text-gray-100">
        {zahl(anzahl)}
      </span>
    </span>
  );
}

/**
 * Balkenreihe der Tage mit Ereignissen. Bewusst reine `div`s statt einer
 * Diagramm-Bibliothek: es sind höchstens ein paar Dutzend Balken ohne Achsen
 * und ohne Interaktion — eine Bibliothek dafür wäre mehr Abhängigkeit als
 * Nutzen. Die Zahlen stehen im `title`, die Reihe insgesamt im `aria-label`.
 */
function Tagesbalken({ tage }: { tage: NutzungSchluessel['tage'] }) {
  const hoechstwert = Math.max(...tage.map((t) => t.aufrufe), 1);
  return (
    <div
      // Die Balken teilen sich die vorhandene Breite (`flex-1`), statt je eine
      // feste Breite zu beanspruchen: bei 365 Tagen oder "Alles" waere die
      // Reihe sonst ueber 3000 px breit und schoebe die Tabelle seitwaerts.
      className="flex items-end gap-px h-12 w-full overflow-hidden"
      role="img"
      aria-label={`Aufrufe an ${tage.length} Tagen, höchstens ${zahl(hoechstwert)} an einem Tag.`}
    >
      {tage.map((t) => (
        <div
          key={t.datum}
          title={`${tagLesbar(t.datum)}: ${zahl(t.aufrufe)} Aufrufe, ${zahl(t.zeichen)} Zeichen`}
          // Mindesthöhe, damit ein einzelner Aufruf neben einem Ausreisser
          // nicht zu einer unsichtbaren Linie zusammenfällt.
          style={{ height: `${Math.max(10, (t.aufrufe / hoechstwert) * 100)}%` }}
          className="flex-1 min-w-[2px] rounded-t bg-blue-500/70 dark:bg-blue-400/70"
        />
      ))}
    </div>
  );
}

/** Aufgeklappter Bereich einer Zeile: Aktionen, Kategorien, Tagesverlauf. */
function Details({ s }: { s: NutzungSchluessel }) {
  const aktionen = Object.entries(s.nachAktion);
  const kategorien = Object.entries(s.nachKategorie);
  return (
    <div className="grid gap-5 px-4 py-4 sm:grid-cols-2 lg:grid-cols-3">
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
          Nach Aktion
        </h4>
        {aktionen.length === 0 ? (
          <p className="text-xs text-gray-400 dark:text-gray-500">Keine Aufrufe.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {aktionen.map(([aktion, anzahl]) => (
              <Chip key={aktion} text={aktionLabel(aktion)} anzahl={anzahl} />
            ))}
          </div>
        )}
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
          Nach Kategorie
        </h4>
        {kategorien.length === 0 ? (
          <p className="text-xs text-gray-400 dark:text-gray-500">
            Keine erkannten Kategorien. Rückführungen zählen keine.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {kategorien.map(([typ, anzahl]) => (
              <Chip key={typ} text={labelFuerTyp(typ)} anzahl={anzahl} />
            ))}
          </div>
        )}
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
          Aufrufe je Tag
        </h4>
        {s.tage.length === 0 ? (
          <p className="text-xs text-gray-400 dark:text-gray-500">Keine Aufrufe.</p>
        ) : (
          <>
            <Tagesbalken tage={s.tage} />
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Erster Aufruf: {datumZeitLesbar(s.erstesEreignis)}
            </p>
          </>
        )}
      </div>
    </div>
  );
}

interface Props {
  /**
   * Wird hochgezählt, wenn im übergeordneten Bereich ein Schlüssel erzeugt
   * oder widerrufen wurde — dann muss die Auswertung neu geladen werden, weil
   * sich die Liste der Schlüssel geändert hat.
   */
  neuladenSignal?: number;
}

/**
 * Abschnitt „Nutzung“: wie stark und wofür jeder Schlüssel benutzt wird.
 *
 * Beantwortet, was „zuletzt benutzt“ nur halb beantwortet — die Grundlage für
 * die Entscheidung, einen Schlüssel zu widerrufen. Gezeigt werden
 * ausschliesslich Zahlen; die Nutzungserfassung im Server enthält keine
 * Inhalte, keine Dateinamen und keine erkannten Werte.
 */
export default function ApiNutzung({ neuladenSignal = 0 }: Props) {
  const [zeitraum, setZeitraum] = useState<number | null>(30);
  const [daten, setDaten] = useState<NutzungDaten | null>(null);
  const [laden, setLaden] = useState(true);
  const [fehler, setFehler] = useState<string | null>(null);
  const [offen, setOffen] = useState<Set<string>>(new Set());

  /**
   * Laufende Nummer der jüngsten Anfrage. Ohne sie gewinnt beim schnellen
   * Durchklicken der Zeiträume die Antwort, die zufällig zuletzt eintrifft —
   * und das muss nicht die zuletzt gestellte sein. Die Tabelle zeigte dann
   * Zahlen eines Zeitraums, der gar nicht mehr ausgewählt ist.
   */
  const juengsteAnfrage = useRef(0);

  const neuLaden = useCallback(async (tage: number | null): Promise<void> => {
    const meine = ++juengsteAnfrage.current;
    setLaden(true);
    setFehler(null);
    try {
      const antwort = await ladeApiNutzung(tage);
      if (meine !== juengsteAnfrage.current) return;
      setDaten(antwort);
    } catch (e) {
      if (meine !== juengsteAnfrage.current) return;
      setFehler(fehlerMeldung(e));
      setDaten(null);
    } finally {
      if (meine === juengsteAnfrage.current) setLaden(false);
    }
  }, []);

  useEffect(() => {
    void neuLaden(zeitraum);
  }, [neuLaden, zeitraum, neuladenSignal]);

  function umschalten(id: string): void {
    setOffen((vorher) => {
      const naechste = new Set(vorher);
      if (naechste.has(id)) naechste.delete(id);
      else naechste.add(id);
      return naechste;
    });
  }

  const schluessel = daten?.schluessel ?? [];
  const gesamtAufrufe = schluessel.reduce((summe, s) => summe + s.aufrufe, 0);

  // Der Zeitraum kommt aus der **Antwort**, nicht aus dem Umschalter: solange
  // eine Anfrage läuft, stehen in der Tabelle noch die alten Zahlen, und die
  // Überschrift soll zu ihnen passen und nicht zur bereits geklickten Auswahl.
  const fensterText =
    daten === null ? null : daten.tage === null ? 'gesamt' : `letzte ${daten.tage} Tage`;

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
          Nutzung{fensterText ? ` — ${fensterText}` : ''}
        </h3>
        <div className="flex items-center gap-3">
          <div
            className="inline-flex rounded-md border border-gray-300 dark:border-gray-600 overflow-hidden"
            role="group"
            aria-label="Zeitraum der Auswertung"
          >
            {ZEITRAEUME.map((z) => {
              const gewaehlt = z.tage === zeitraum;
              return (
                <button
                  key={z.label}
                  type="button"
                  onClick={() => setZeitraum(z.tage)}
                  aria-pressed={gewaehlt}
                  className={`px-2.5 py-1 text-xs border-r last:border-r-0 border-gray-300 dark:border-gray-600 transition-colors ${
                    gewaehlt
                      ? 'bg-blue-600 text-white'
                      : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
                  }`}
                >
                  {z.label}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            onClick={() => void neuLaden(zeitraum)}
            className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Neu laden
          </button>
        </div>
      </div>

      <p className="text-sm text-gray-500 dark:text-gray-400">
        Gezählt werden ausschliesslich{' '}
        <strong className="font-medium">Verarbeitungsaufrufe</strong> — Erkennen,
        Pseudonymisieren und Rückführen. Reine Lese-Aufrufe (Kategorienkatalog,
        Zuordnungstabellen, Begriffsliste) erscheinen hier nicht, wohl aber in der Spalte
        „Zuletzt benutzt“ der Schlüsseltabelle oben. Erfasst werden dabei nur Zahlen: wie viele
        Aufrufe, welche Aktionen, wie viele Treffer je Kategorie. Weder Texte noch Dateinamen
        noch erkannte Werte werden gespeichert.
      </p>

      {fehler && <Hinweis ton="fehler">{fehler}</Hinweis>}

      {laden ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">Nutzung wird geladen …</p>
      ) : schluessel.length === 0 ? (
        <p className="rounded-lg border border-dashed border-gray-300 dark:border-gray-600 px-4 py-8 text-center text-sm text-gray-400 dark:text-gray-500">
          Es sind noch keine API-Schlüssel ausgestellt.
        </p>
      ) : (
        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
                <tr>
                  <th className="px-4 py-3" scope="col">
                    <span className="sr-only">Details</span>
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Bezeichnung
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Status
                  </th>
                  <th
                    className="text-right px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Aufrufe
                  </th>
                  <th
                    className="text-right px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Zeichen
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Letzter Aufruf
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {schluessel.map((s) => {
                  const istOffen = offen.has(s.id);
                  return [
                    <tr key={s.id} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                      <td className="px-4 py-3 align-top">
                        <button
                          type="button"
                          onClick={() => umschalten(s.id)}
                          aria-expanded={istOffen}
                          className="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100"
                        >
                          {istOffen ? (
                            <ChevronDown className="w-4 h-4" />
                          ) : (
                            <ChevronRight className="w-4 h-4" />
                          )}
                          <span className="sr-only">
                            {istOffen ? 'Details ausblenden' : 'Details anzeigen'} für {s.label}
                          </span>
                        </button>
                      </td>
                      <td className="px-4 py-3 font-medium text-gray-900 dark:text-gray-100 break-all">
                        {s.label}
                        <span className="ml-2 font-mono text-xs text-gray-400 dark:text-gray-500">
                          {s.keyPrefix}…
                        </span>
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <StatusBadge status={schluesselStatus(s)} />
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-900 dark:text-gray-100">
                        {zahl(s.aufrufe)}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300">
                        {zahl(s.zeichen)}
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-gray-300 whitespace-nowrap">
                        {datumZeitLesbar(s.letztesEreignis)}
                      </td>
                    </tr>,
                    istOffen ? (
                      <tr key={`${s.id}-details`} className="bg-gray-50 dark:bg-gray-700/30">
                        <td colSpan={6}>
                          <Details s={s} />
                        </td>
                      </tr>
                    ) : null,
                  ];
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!laden && schluessel.length > 0 && gesamtAufrufe === 0 && (
        <p className="text-xs text-gray-400 dark:text-gray-500">
          In diesem Zeitraum wurde keiner dieser Schlüssel benutzt.
        </p>
      )}

      {!laden && daten?.tage === null && (
        <p className="text-xs text-gray-400 dark:text-gray-500">
          Aufrufe aus der Zeit vor der Umstellung auf die Zuordnung per Schlüssel-Kennung fehlen
          in dieser Auswertung. Sie liessen sich nicht verlässlich nachtragen, weil Bezeichnungen
          — anders als Kennungen — nicht eindeutig sind.
        </p>
      )}
    </section>
  );
}
