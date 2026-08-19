import type { Kategorie } from '../core/api';

interface Props {
  kategorien: Kategorie[];
  /** Entity-Typen, die ersetzt werden sollen. */
  gewaehlt: Set<string>;
  onGewaehlt: (gewaehlt: Set<string>) => void;
  deaktiviert?: boolean;
}

/**
 * Checkbox-Auswahl der Entitätstypen vor dem Lauf.
 *
 * Der Katalog kommt vom Server statt aus einer lokalen Kopie — neue Erkenner
 * erscheinen hier automatisch. Die Auswahl wird vom Panel immer explizit als
 * `entities` mitgeschickt (auch KEEP-Typen wie PLZ, wenn angehakt): eine
 * ausdrückliche Wahl schlägt beim Server die Default-Policy, „nichts
 * ausgewählt“ ist kein „alles“.
 */
export default function KategorienWahl({ kategorien, gewaehlt, onGewaehlt, deaktiviert }: Props) {
  function umschalten(typ: string): void {
    const neu = new Set(gewaehlt);
    if (neu.has(typ)) neu.delete(typ);
    else neu.add(typ);
    onGewaehlt(neu);
  }

  function alleSetzen(an: boolean): void {
    onGewaehlt(an ? new Set(kategorien.map((k) => k.entity_type)) : new Set());
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">Kategorien</h3>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => alleSetzen(true)}
            disabled={deaktiviert}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50"
          >
            Alle
          </button>
          <button
            type="button"
            onClick={() => alleSetzen(false)}
            disabled={deaktiviert}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50"
          >
            Keine
          </button>
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {kategorien.map((k) => {
          const an = gewaehlt.has(k.entity_type);
          return (
            <label
              key={k.entity_type}
              title={k.description}
              className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
                deaktiviert ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
              } ${
                an
                  ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                  : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50'
              }`}
            >
              <input
                type="checkbox"
                checked={an}
                onChange={() => umschalten(k.entity_type)}
                disabled={deaktiviert}
                className="mt-0.5 h-4 w-4 shrink-0 text-blue-600 border-gray-300 dark:border-gray-600 rounded focus:ring-blue-500"
              />
              <span className="min-w-0">
                <span className="block text-gray-900 dark:text-gray-100">{k.label}</span>
                {k.default_policy === 'keep' && (
                  <span className="block text-xs text-gray-500 dark:text-gray-400">
                    (mehrdeutig — standardmässig nicht ausgewählt)
                  </span>
                )}
              </span>
            </label>
          );
        })}
      </div>

      <p className="text-xs text-gray-500 dark:text-gray-400">
        {gewaehlt.size} von {kategorien.length} gewählt
      </p>
    </div>
  );
}
