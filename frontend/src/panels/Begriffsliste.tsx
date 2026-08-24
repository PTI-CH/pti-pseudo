import { useEffect, useState } from 'react';
import { Loader2, Plus, RefreshCw } from 'lucide-react';
import Hinweis from '../components/Hinweis';
import {
  fehlerMeldung,
  ladeBegriffe,
  legeBegriffAn,
  loescheBegriff,
  type Begriff,
} from '../core/api';
import { ENTITAETS_TYPEN, labelFuerTyp } from '../core/entities';

interface Props {
  /**
   * Ob der angemeldete Mensch die Liste ändern darf.
   *
   * Kommt als Eigenschaft und nicht aus einem zweiten `useBenutzer()`: `App`
   * lädt den Benutzer bereits, ein weiterer Aufruf hier fragte `/api/me` ein
   * zweites Mal an.
   */
  istAdmin: boolean;
}

/**
 * Bereich 3: eigene Begriffe, die die Engine immer ersetzt.
 *
 * Gedacht für Projektnamen, interne Bezeichnungen und Eigennamen, die kein
 * allgemeiner Erkenner findet — die Liste hat Vorrang vor der Statistik.
 *
 * **Lesen darf jeder, ändern nur Administratoren.** Das Backend setzt das
 * durch (`403` in `routers/terms.py`); hier werden die Bedienelemente
 * ausgeblendet, statt sie anzubieten und dann scheitern zu lassen. Ein Knopf,
 * der zuverlässig einen Fehler erzeugt, ist schlimmer als keiner: Er sieht aus
 * wie eine kaputte Anwendung, nicht wie eine fehlende Berechtigung.
 */
export default function Begriffsliste({ istAdmin }: Props) {
  const [begriffe, setBegriffe] = useState<Begriff[]>([]);
  const [laden, setLaden] = useState(true);
  const [fehler, setFehler] = useState<string | null>(null);

  const [neuerBegriff, setNeuerBegriff] = useState('');
  const [neuerTyp, setNeuerTyp] = useState<string>('PERSON');
  const [neuerErsatz, setNeuerErsatz] = useState('');
  const [speichert, setSpeichert] = useState(false);
  /** Kennung des Begriffs, der gerade gelöscht wird. */
  const [loescht, setLoescht] = useState<string | null>(null);

  async function neuLaden(): Promise<void> {
    setLaden(true);
    setFehler(null);
    try {
      setBegriffe(await ladeBegriffe());
    } catch (e) {
      setFehler(fehlerMeldung(e));
      setBegriffe([]);
    } finally {
      setLaden(false);
    }
  }

  useEffect(() => {
    void neuLaden();
  }, []);

  async function anlegen(): Promise<void> {
    const wert = neuerBegriff.trim();
    if (!wert) return;
    setSpeichert(true);
    setFehler(null);
    try {
      await legeBegriffAn(wert, neuerTyp, neuerErsatz.trim() || null);
      setNeuerBegriff('');
      setNeuerErsatz('');
      await neuLaden();
    } catch (e) {
      setFehler(fehlerMeldung(e));
    } finally {
      setSpeichert(false);
    }
  }

  async function entfernen(id: string, bezeichnung: string): Promise<void> {
    if (!window.confirm(`Soll der Begriff „${bezeichnung}“ wirklich gelöscht werden?`)) return;
    setLoescht(id);
    setFehler(null);
    try {
      await loescheBegriff(id);
      await neuLaden();
    } catch (e) {
      setFehler(fehlerMeldung(e));
    } finally {
      setLoescht(null);
    }
  }

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Begriffsliste</h2>
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
          Begriffe auf dieser Liste werden in jedem Durchlauf ersetzt — unabhängig davon, ob ein
          Erkenner sie findet. Typisch sind Projektnamen, interne Bezeichnungen, Deckname oder
          Aktenzeichen.
        </p>
      </section>

      {fehler && <Hinweis ton="fehler">{fehler}</Hinweis>}

      {!istAdmin && (
        <Hinweis ton="info">
          Diese Liste wirkt auf <strong>jeden</strong> Durchlauf, auch auf Ihre. Pflegen
          darf sie, wer im CRM die Rolle „admin“ hat — melden Sie fehlende oder falsche
          Einträge dort.
        </Hinweis>
      )}

      {/* Neuen Begriff anlegen — nur für Administratoren */}
      {istAdmin && (
      <section className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-4">
        <div className="grid gap-4 sm:grid-cols-3">
          <div>
            <label
              htmlFor="begriff"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Begriff *
            </label>
            <input
              id="begriff"
              type="text"
              value={neuerBegriff}
              onChange={(e) => setNeuerBegriff(e.target.value)}
              placeholder="z.B. Projekt Nordwind"
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label
              htmlFor="typ"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Behandeln als
            </label>
            <select
              id="typ"
              value={neuerTyp}
              onChange={(e) => setNeuerTyp(e.target.value)}
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {ENTITAETS_TYPEN.map((t) => (
                <option key={t} value={t}>
                  {labelFuerTyp(t)}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor="ersatz"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Festes Pseudonym
            </label>
            <input
              id="ersatz"
              type="text"
              value={neuerErsatz}
              onChange={(e) => setNeuerErsatz(e.target.value)}
              maxLength={100}
              placeholder="optional, z. B. [[LIEFERANT_A]]"
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            {/* Der Hinweis ist keine Formalie: ein Ersatztext, der auch als
                gewöhnliche Wortfolge vorkommen kann, wird beim Rückführen
                überall im Dokument durch den Klarnamen ersetzt — auch dort, wo
                er harmlos gemeint war. Die doppelte Klammerform kommt in
                natürlichem Text praktisch nicht vor. */}
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Leer lassen für ein automatisches <code>[[TYP_1]]</code>. Wählen Sie sonst
              etwas, das im Fliesstext nie vorkommt — bei „Firma A“ würde die Rückführung
              jedes „Firma A“ im Dokument in den Klarnamen zurückverwandeln.
            </p>
          </div>
        </div>
        <div className="flex justify-end mt-4">
          <button
            type="button"
            onClick={() => void anlegen()}
            disabled={!neuerBegriff.trim() || speichert}
            className="inline-flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {speichert ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            {speichert ? 'Wird gespeichert …' : 'Hinzufügen'}
          </button>
        </div>
      </section>
      )}

      {/* Bestehende Begriffe */}
      {laden ? (
        <p className="text-gray-500 dark:text-gray-400 text-sm">Begriffe werden geladen …</p>
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
                    Begriff
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Behandelt als
                  </th>
                  <th
                    className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                    scope="col"
                  >
                    Festes Pseudonym
                  </th>
                  <th className="px-4 py-3" scope="col">
                    <span className="sr-only">Aktionen</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {begriffe.map((b) => (
                  <tr key={b.id} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                    <td className="px-4 py-3 font-medium text-gray-900 dark:text-gray-100 break-all">
                      {b.begriff}
                    </td>
                    <td className="px-4 py-3 text-gray-700 dark:text-gray-300 whitespace-nowrap">
                      {labelFuerTyp(b.typ)}
                    </td>
                    <td className="px-4 py-3 text-gray-500 dark:text-gray-400 font-mono text-xs break-all">
                      {b.ersatz || <span className="font-sans">automatisch</span>}
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      {istAdmin && (
                        <button
                          type="button"
                          onClick={() => void entfernen(b.id, b.begriff)}
                          disabled={loescht === b.id}
                          className="text-xs text-red-600 dark:text-red-400 hover:underline disabled:opacity-50"
                        >
                          {loescht === b.id ? 'Wird gelöscht …' : 'Löschen'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {begriffe.length === 0 && (
                  <tr>
                    <td
                      colSpan={4}
                      className="px-4 py-8 text-center text-gray-400 dark:text-gray-500"
                    >
                      Es sind noch keine eigenen Begriffe erfasst.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
