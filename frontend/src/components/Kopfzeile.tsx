import { BookOpen, Moon, Sun, UserRound } from 'lucide-react';
import Logo from './Logo';
import { anzeigeName, rollenLabel, type Benutzer } from '../core/me';

/** Adresse des Fachkonzepts (§1–§15) im Schwester-Repo `landing`. */
const FACHKONZEPT_URL = 'https://pti-tip-hub.ch/ideen/pseudonymizer.html';

interface Props {
  dunkel: boolean;
  onDunkel: (dunkel: boolean) => void;
  /**
   * Von `App` durchgereicht statt hier per `useBenutzer()` selbst geladen:
   * `App` braucht denselben Benutzer für die Admin-Bereichswahl, ein zweiter
   * Aufruf hier würde `/api/me` doppelt anfragen.
   */
  benutzer: Benutzer;
}

export default function Kopfzeile({ dunkel, onDunkel, benutzer }: Props) {
  const name = anzeigeName(benutzer);
  const rolle = rollenLabel(benutzer.role);

  return (
    <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-4 sm:px-6 py-3 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <Logo />
      <div className="flex flex-wrap items-center justify-end gap-2">
        {/* Der Benutzer darf fehlen — /api/me kann fehlschlagen. */}
        {name && (
          <span className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-full bg-gray-50 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
            <UserRound className="w-4 h-4 shrink-0" />
            <span className="max-w-[12rem] truncate">{name}</span>
            {rolle && (
              <span className="text-xs text-gray-400 dark:text-gray-500 hidden sm:inline">
                ({rolle})
              </span>
            )}
          </span>
        )}
        <a
          href={FACHKONZEPT_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 dark:text-gray-300 rounded-full hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
          title="Fachkonzept im TIP-Hub öffnen"
        >
          <BookOpen className="w-4 h-4" />
          <span className="hidden sm:inline">Fachkonzept</span>
        </a>
        <button
          type="button"
          onClick={() => onDunkel(!dunkel)}
          className="p-2 rounded-full border border-gray-200 dark:border-gray-600 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          aria-label={dunkel ? 'Helles Erscheinungsbild' : 'Dunkles Erscheinungsbild'}
        >
          {dunkel ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
        </button>
      </div>
    </header>
  );
}
