import { useEffect, useState } from 'react';
import { Check, Copy, Loader2, Plus, RefreshCw } from 'lucide-react';
import Hinweis from '../components/Hinweis';
import {
  erzeugeApiSchluessel,
  fehlerMeldung,
  ladeApiSchluessel,
  widerrufeApiSchluessel,
  type ApiSchluessel,
} from '../core/api';

/** Höchstlänge der Bezeichnung — spiegelt `label String(200)` im Server, mit Reserve. */
const BEZEICHNUNG_MAX = 140;

type Status = 'aktiv' | 'widerrufen' | 'abgelaufen';

/**
 * Status wird bewusst im Frontend abgeleitet statt nur `aktiv` aus der API zu
 * übernehmen: „abgelaufen“ ist ein reiner Zeitvergleich (`expiresAt` in der
 * Vergangenheit, nicht widerrufen) und muss sich beim blossen Neuladen der
 * Seite aktualisieren, ohne dass der Server dafür etwas Neues schreibt.
 */
function status(s: ApiSchluessel): Status {
  if (s.revokedAt) return 'widerrufen';
  if (s.expiresAt && new Date(s.expiresAt).getTime() < Date.now()) return 'abgelaufen';
  return 'aktiv';
}

const STATUS_LABEL: Record<Status, string> = {
  aktiv: 'Aktiv',
  widerrufen: 'Widerrufen',
  abgelaufen: 'Abgelaufen',
};

const STATUS_FARBE: Record<Status, string> = {
  aktiv: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  widerrufen: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  abgelaufen: 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400',
};

function datumLesbar(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('de-CH', { dateStyle: 'short', timeStyle: 'short' });
}

/**
 * `<input type="date">` liefert `YYYY-MM-DD` ohne Uhrzeit. Ein Ablaufdatum
 * soll den ganzen gewählten Tag noch gültig lassen — deshalb Tagesende UTC
 * statt Mitternacht, sonst wäre der Schlüssel schon am Morgen des gewählten
 * Tages abgelaufen.
 */
function ablaufAlsIso(datum: string): string | null {
  if (!datum) return null;
  return `${datum}T23:59:59.999Z`;
}

const CURL_BEISPIEL = `curl -X POST https://pseudo.pti-tip-hub.ch/api/ext/v1/documents:anonymize \\
  -H "X-Api-Key: ptps_…" -H "Content-Type: application/json" \\
  -d '{"text": "Herr Andreas Meier, 8001 Zürich", "mode": "reversible", "entities": ["PERSON","CH_IBAN"]}'`;

/** Bereich 5 (nur Admin): externe API-Schlüssel für andere PTI-Anwendungen ausstellen und widerrufen. */
export default function ApiSchluesselPanel() {
  const [schluessel, setSchluessel] = useState<ApiSchluessel[]>([]);
  const [laden, setLaden] = useState(true);
  const [fehler, setFehler] = useState<string | null>(null);

  const [neueBezeichnung, setNeueBezeichnung] = useState('');
  const [neuesAblaufdatum, setNeuesAblaufdatum] = useState('');
  const [erzeugt, setErzeugt] = useState(false);
  const [widerruft, setWiderruft] = useState<string | null>(null);

  /** Der Klartext-Token — nur direkt nach dem Erzeugen bekannt, danach nie wieder. */
  const [neuerToken, setNeuerToken] = useState<string | null>(null);
  const [tokenKopiert, setTokenKopiert] = useState(false);

  async function neuLaden(): Promise<void> {
    setLaden(true);
    setFehler(null);
    try {
      setSchluessel(await ladeApiSchluessel());
    } catch (e) {
      setFehler(fehlerMeldung(e));
      setSchluessel([]);
    } finally {
      setLaden(false);
    }
  }

  useEffect(() => {
    void neuLaden();
  }, []);

  const bezeichnungGueltig =
    neueBezeichnung.trim().length > 0 && neueBezeichnung.trim().length <= BEZEICHNUNG_MAX;

  async function anlegen(): Promise<void> {
    if (!bezeichnungGueltig) return;
    setErzeugt(true);
    setFehler(null);
    try {
      const { token } = await erzeugeApiSchluessel(
        neueBezeichnung.trim(),
        ablaufAlsIso(neuesAblaufdatum),
      );
      setNeuerToken(token);
      setTokenKopiert(false);
      setNeueBezeichnung('');
      setNeuesAblaufdatum('');
      await neuLaden();
    } catch (e) {
      setFehler(fehlerMeldung(e));
    } finally {
      setErzeugt(false);
    }
  }

  async function tokenKopieren(): Promise<void> {
    if (!neuerToken) return;
    try {
      await navigator.clipboard.writeText(neuerToken);
      setTokenKopiert(true);
      window.setTimeout(() => setTokenKopiert(false), 1500);
    } catch {
      // Zwischenablage kann vom Browser gesperrt sein — der Token steht im
      // Feld und lässt sich weiterhin von Hand markieren und kopieren.
    }
  }

  async function widerrufen(id: string, bezeichnung: string): Promise<void> {
    if (
      !window.confirm(
        `Soll der Schlüssel „${bezeichnung}“ wirklich widerrufen werden? Anwendungen, die ihn verwenden, verlieren sofort den Zugriff.`,
      )
    ) {
      return;
    }
    setWiderruft(id);
    setFehler(null);
    try {
      await widerrufeApiSchluessel(id);
      await neuLaden();
    } catch (e) {
      setFehler(fehlerMeldung(e));
    } finally {
      setWiderruft(null);
    }
  }

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            API-Schlüssel
          </h2>
          <button
            type="button"
            onClick={() => void neuLaden()}
            className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Neu laden
          </button>
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Schlüssel geben anderen PTI-Anwendungen Zugriff auf die externe API dieses Werkzeugs —
          ohne TIP-Hub-Anmeldung, mit vollen Rechten innerhalb dieses Mandanten. Ein widerrufener
          Schlüssel bleibt zur Nachvollziehbarkeit in der Liste sichtbar.
        </p>
      </section>

      {fehler && <Hinweis ton="fehler">{fehler}</Hinweis>}

      {neuerToken && (
        <Hinweis ton="warnung">
          <p className="font-medium">Dieser Schlüssel wird nur jetzt angezeigt.</p>
          <p className="mt-1">Notieren Sie ihn in der aufrufenden Anwendung.</p>
          <div className="mt-2 flex items-center gap-3 flex-wrap">
            <code className="break-all rounded bg-white dark:bg-gray-800 border border-amber-200 dark:border-amber-700 px-2 py-1 text-xs text-gray-900 dark:text-gray-100">
              {neuerToken}
            </code>
            <button
              type="button"
              onClick={() => void tokenKopieren()}
              className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
            >
              {tokenKopiert ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
              {tokenKopiert ? 'Kopiert' : 'Kopieren'}
            </button>
          </div>
          <button
            type="button"
            onClick={() => setNeuerToken(null)}
            className="mt-2 text-xs text-amber-700 dark:text-amber-400 hover:underline"
          >
            Ausblenden
          </button>
        </Hinweis>
      )}

      {/* Neuen Schlüssel erzeugen */}
      <section className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-4">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">
          Neuen Schlüssel erzeugen
        </h3>
        <div className="grid gap-4 sm:grid-cols-3">
          <div className="sm:col-span-2">
            <label
              htmlFor="schluessel-bezeichnung"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Bezeichnung *
            </label>
            <input
              id="schluessel-bezeichnung"
              type="text"
              value={neueBezeichnung}
              onChange={(e) => setNeueBezeichnung(e.target.value)}
              maxLength={BEZEICHNUNG_MAX}
              placeholder="z.B. Translator"
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label
              htmlFor="schluessel-ablauf"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Ablaufdatum
            </label>
            <input
              id="schluessel-ablauf"
              type="date"
              value={neuesAblaufdatum}
              onChange={(e) => setNeuesAblaufdatum(e.target.value)}
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>
        <div className="flex justify-end mt-4">
          <button
            type="button"
            onClick={() => void anlegen()}
            disabled={!bezeichnungGueltig || erzeugt}
            className="inline-flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {erzeugt ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            {erzeugt ? 'Wird erzeugt …' : 'Schlüssel erzeugen'}
          </button>
        </div>
      </section>

      {/* Bestehende Schlüssel */}
      {laden ? (
        <p className="text-gray-500 dark:text-gray-400 text-sm">Schlüssel werden geladen …</p>
      ) : (
        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
                <tr>
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
                    Präfix
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Erstellt von/am
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Zuletzt benutzt
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Status
                  </th>
                  <th className="px-4 py-3" scope="col">
                    <span className="sr-only">Aktionen</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {schluessel.map((s) => {
                  const st = status(s);
                  return (
                    <tr key={s.id} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                      <td className="px-4 py-3 font-medium text-gray-900 dark:text-gray-100 break-all">
                        {s.label}
                      </td>
                      <td className="px-4 py-3 text-gray-500 dark:text-gray-400 font-mono text-xs whitespace-nowrap">
                        {s.keyPrefix}…
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-gray-300 whitespace-nowrap">
                        {s.createdBy} · {datumLesbar(s.createdAt)}
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-gray-300 whitespace-nowrap">
                        {s.lastUsedAt ? datumLesbar(s.lastUsedAt) : 'nie'}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <span
                          className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_FARBE[st]}`}
                        >
                          {STATUS_LABEL[st]}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right whitespace-nowrap">
                        {st === 'aktiv' && (
                          <button
                            type="button"
                            onClick={() => void widerrufen(s.id, s.label)}
                            disabled={widerruft === s.id}
                            className="text-xs text-red-600 dark:text-red-400 hover:underline disabled:opacity-50"
                          >
                            {widerruft === s.id ? 'Wird widerrufen …' : 'Widerrufen'}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {schluessel.length === 0 && (
                  <tr>
                    <td
                      colSpan={6}
                      className="px-4 py-8 text-center text-gray-400 dark:text-gray-500"
                    >
                      Es sind noch keine API-Schlüssel ausgestellt.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <Hinweis ton="info">
        <p className="font-medium">Aufruf der externen API</p>
        <pre className="mt-2 w-full overflow-auto whitespace-pre-wrap break-words rounded border border-blue-200 dark:border-blue-800 bg-white dark:bg-gray-800 px-3 py-2 text-xs font-mono text-gray-900 dark:text-gray-100">
          {CURL_BEISPIEL}
        </pre>
        <p className="mt-2">
          Vollständige Beschreibung aller Endpunkte, Fehlerformate und Beispiele in{' '}
          <code className="text-xs">API.md</code> im Repository sowie unter{' '}
          <code className="text-xs">/api/docs</code>.
        </p>
      </Hinweis>
    </div>
  );
}
