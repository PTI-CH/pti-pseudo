import { AlertTriangle, Info, ShieldAlert } from 'lucide-react';
import type { ReactNode } from 'react';

export type HinweisTon = 'fehler' | 'warnung' | 'info';

// Farben immer per Record nachschlagen, nie inline — so bleiben die drei Töne
// über alle Bereiche hinweg identisch.
const TON_KLASSEN: Record<HinweisTon, string> = {
  fehler:
    'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800 text-red-700 dark:text-red-400',
  warnung:
    'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-700 text-amber-700 dark:text-amber-400',
  info: 'bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-400',
};

const TON_SYMBOL: Record<HinweisTon, typeof Info> = {
  fehler: ShieldAlert,
  warnung: AlertTriangle,
  info: Info,
};

interface Props {
  ton: HinweisTon;
  children: ReactNode;
  className?: string;
}

/** Meldungsband für Fehler, Warnungen und Erklärungen. */
export default function Hinweis({ ton, children, className = '' }: Props) {
  const Symbol = TON_SYMBOL[ton];
  return (
    <div
      className={`flex items-start gap-2 rounded-lg border px-4 py-3 text-sm ${TON_KLASSEN[ton]} ${className}`}
      role={ton === 'fehler' ? 'alert' : undefined}
    >
      <Symbol className="w-4 h-4 shrink-0 mt-0.5" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}
