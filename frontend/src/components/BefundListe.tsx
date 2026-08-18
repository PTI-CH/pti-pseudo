import { useMemo } from 'react';
import { Loader2 } from 'lucide-react';
import type { Befund } from '../core/api';
import {
  AMPEL_FARBE,
  AMPEL_PUNKT,
  AMPEL_TEXT,
  ampelStufe,
  konfidenzProzent,
  labelFuerTyp,
  type AmpelStufe,
} from '../core/entities';

interface Props {
  befunde: Befund[];
  /** Kennungen der Funde, die ersetzt werden sollen. */
  ausgewaehlt: Set<string>;
  onAusgewaehlt: (ausgewaehlt: Set<string>) => void;
  onAnwenden: () => void;
  laeuft: boolean;
}

const REIHENFOLGE: Record<AmpelStufe, number> = { rot: 0, gelb: 1, gruen: 2 };

/**
 * Befund-Ansicht mit Verlässlichkeits-Ampel.
 *
 * Die Liste ist bewusst nach Ampelstufe sortiert: rot und gelb zuerst. Genau
 * diese Funde muss ein Mensch prüfen — grün darf überflogen werden. Wer einen
 * Fehlalarm findet, wählt ihn ab und drückt „Anwenden“.
 */
export default function BefundListe({
  befunde,
  ausgewaehlt,
  onAusgewaehlt,
  onAnwenden,
  laeuft,
}: Props) {
  const sortiert = useMemo(
    () =>
      [...befunde].sort((a, b) => {
        const d = REIHENFOLGE[ampelStufe(a.score)] - REIHENFOLGE[ampelStufe(b.score)];
        return d !== 0 ? d : a.start - b.start;
      }),
    [befunde],
  );

  const zaehler = useMemo(() => {
    const z: Record<AmpelStufe, number> = { gruen: 0, gelb: 0, rot: 0 };
    befunde.forEach((b) => {
      z[ampelStufe(b.score)] += 1;
    });
    return z;
  }, [befunde]);

  const zuPruefen = zaehler.rot + zaehler.gelb;

  function umschalten(id: string): void {
    const neu = new Set(ausgewaehlt);
    if (neu.has(id)) neu.delete(id);
    else neu.add(id);
    onAusgewaehlt(neu);
  }

  function alleSetzen(an: boolean): void {
    onAusgewaehlt(an ? new Set(befunde.map((b) => b.id)) : new Set());
  }

  if (befunde.length === 0) {
    return (
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-8 text-center text-sm text-gray-400 dark:text-gray-500">
        Es wurden keine Personendaten gefunden.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Ampel-Zusammenfassung */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-gray-500 dark:text-gray-400">
        <span className="flex items-center gap-1.5">
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${AMPEL_PUNKT.gruen}`} />
          {zaehler.gruen} sicher (ab 85 %)
        </span>
        <span className="flex items-center gap-1.5">
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${AMPEL_PUNKT.gelb}`} />
          {zaehler.gelb} zu prüfen (50–85 %)
        </span>
        <span className="flex items-center gap-1.5">
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${AMPEL_PUNKT.rot}`} />
          {zaehler.rot} unsicher (unter 50 %)
        </span>
        <span className="ml-auto flex items-center gap-3">
          <button
            type="button"
            onClick={() => alleSetzen(true)}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            Alle auswählen
          </button>
          <button
            type="button"
            onClick={() => alleSetzen(false)}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            Keine
          </button>
        </span>
      </div>

      {zuPruefen > 0 && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {zuPruefen} {zuPruefen === 1 ? 'Fund ist' : 'Funde sind'} unsicher und stehen zuoberst.
          Bitte prüfen Sie diese und wählen Sie Fehlalarme ab.
        </p>
      )}

      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
              <tr>
                <th className="w-10 px-4 py-3" scope="col">
                  <span className="sr-only">Ersetzen</span>
                </th>
                <th
                  className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                  scope="col"
                >
                  Typ
                </th>
                <th
                  className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                  scope="col"
                >
                  Gefundener Text
                </th>
                <th
                  className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300"
                  scope="col"
                >
                  Pseudonym
                </th>
                <th
                  className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300 whitespace-nowrap"
                  scope="col"
                >
                  Verlässlichkeit
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
              {sortiert.map((b) => {
                const stufe = ampelStufe(b.score);
                const an = ausgewaehlt.has(b.id);
                return (
                  <tr
                    key={b.id}
                    className={`hover:bg-gray-50 dark:hover:bg-gray-700/50 ${an ? '' : 'opacity-50'}`}
                  >
                    <td className="px-4 py-3">
                      <input
                        type="checkbox"
                        checked={an}
                        onChange={() => umschalten(b.id)}
                        className="h-4 w-4 text-blue-600 border-gray-300 dark:border-gray-600 rounded focus:ring-blue-500"
                        aria-label={`${labelFuerTyp(b.entity_type)} „${b.text}“ ersetzen`}
                      />
                    </td>
                    <td className="px-4 py-3 text-gray-700 dark:text-gray-300 whitespace-nowrap">
                      {labelFuerTyp(b.entity_type)}
                    </td>
                    <td className="px-4 py-3 font-medium text-gray-900 dark:text-gray-100 break-all">
                      {b.text || <span className="text-gray-400 dark:text-gray-500">—</span>}
                    </td>
                    <td className="px-4 py-3 text-gray-500 dark:text-gray-400 font-mono text-xs break-all">
                      {b.replacement || '—'}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <span
                        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${AMPEL_FARBE[stufe]}`}
                        title={`Konfidenz ${konfidenzProzent(b.score)}`}
                      >
                        <span className={`inline-block w-2 h-2 rounded-full ${AMPEL_PUNKT[stufe]}`} />
                        {konfidenzProzent(b.score)} · {AMPEL_TEXT[stufe]}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {ausgewaehlt.size} von {befunde.length} Funden werden ersetzt.
        </p>
        <button
          type="button"
          onClick={onAnwenden}
          disabled={laeuft}
          className="inline-flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          {laeuft && <Loader2 className="w-4 h-4 animate-spin" />}
          {laeuft ? 'Wird angewendet …' : 'Anwenden'}
        </button>
      </div>
    </div>
  );
}
