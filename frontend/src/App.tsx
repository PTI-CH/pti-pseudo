import { useEffect, useMemo, useState } from 'react';
import { BookMarked, ShieldCheck, Undo2, Webhook } from 'lucide-react';
import Kopfzeile from './components/Kopfzeile';
import Pseudonymisieren from './panels/Pseudonymisieren';
import Rueckfuehren from './panels/Rueckfuehren';
import Begriffsliste from './panels/Begriffsliste';
import ApiSchluesselPanel from './panels/ApiSchluessel';
import { useBenutzer } from './core/me';

type Bereich = 'pseudonymisieren' | 'rueckfuehren' | 'begriffe' | 'api-schluessel';

const BEREICHE = [
  { id: 'pseudonymisieren', beschriftung: 'Pseudonymisieren', Symbol: ShieldCheck },
  { id: 'rueckfuehren', beschriftung: 'Rückführen', Symbol: Undo2 },
  { id: 'begriffe', beschriftung: 'Begriffsliste', Symbol: BookMarked },
  { id: 'api-schluessel', beschriftung: 'API-Schlüssel', Symbol: Webhook },
] as const satisfies readonly { id: Bereich; beschriftung: string; Symbol: typeof ShieldCheck }[];

export default function App() {
  // Der Startwert kommt aus dem DOM: main.tsx hat die Klasse bereits VOR dem
  // ersten Render gesetzt, hier wird sie nur noch gespiegelt.
  const [dunkel, setDunkel] = useState(() => document.documentElement.classList.contains('dark'));
  const [bereich, setBereich] = useState<Bereich>('pseudonymisieren');

  // Einmal hier geladen und an Kopfzeile durchgereicht (Anzeige) sowie für die
  // Admin-Bereichswahl genutzt — ein zweiter `useBenutzer()` in Kopfzeile
  // würde `/api/me` doppelt anfragen.
  const benutzer = useBenutzer();

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dunkel);
    localStorage.setItem('pti-theme', dunkel ? 'dark' : 'light');
  }, [dunkel]);

  // Verlässt ein Admin den Bereich (Rolle wechselt serverseitig, oder /api/me
  // schlägt erst nach der Erstwahl fehl), landet die Ansicht nicht auf einem
  // nun unsichtbaren Bereich.
  useEffect(() => {
    if (bereich === 'api-schluessel' && !benutzer.isAdmin) {
      setBereich('pseudonymisieren');
    }
  }, [benutzer.isAdmin, bereich]);

  const sichtbareBereiche = useMemo(
    () => BEREICHE.filter((b) => b.id !== 'api-schluessel' || benutzer.isAdmin),
    [benutzer.isAdmin],
  );

  return (
    <div className="min-h-screen flex flex-col bg-gray-50 dark:bg-gray-900">
      <Kopfzeile dunkel={dunkel} onDunkel={setDunkel} benutzer={benutzer} />

      {/* Bereichswahl */}
      <nav className="px-4 sm:px-6 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
        <div className="max-w-5xl mx-auto flex gap-1 overflow-x-auto">
          {sichtbareBereiche.map(({ id, beschriftung, Symbol }) => {
            const aktiv = bereich === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => setBereich(id)}
                aria-current={aktiv ? 'page' : undefined}
                className={`inline-flex items-center gap-1.5 whitespace-nowrap px-4 py-3 text-sm border-b-2 -mb-px transition-colors ${
                  aktiv
                    ? 'border-blue-600 text-blue-700 dark:text-blue-400 font-medium'
                    : 'border-transparent text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
                }`}
              >
                <Symbol className="w-4 h-4" />
                {beschriftung}
              </button>
            );
          })}
        </div>
      </nav>

      <main className="flex-1 px-4 sm:px-6 py-6">
        <div className="max-w-5xl mx-auto">
          {/* Alle drei Bereiche bleiben montiert: wer zwischen Pseudonymisieren
              und Begriffsliste wechselt, findet seinen Text und seine Befunde
              danach unverändert vor. */}
          <div className={bereich === 'pseudonymisieren' ? '' : 'hidden'}>
            <Pseudonymisieren />
          </div>
          <div className={bereich === 'rueckfuehren' ? '' : 'hidden'}>
            <Rueckfuehren />
          </div>
          <div className={bereich === 'begriffe' ? '' : 'hidden'}>
            <Begriffsliste />
          </div>
          {/* Nur montiert, wenn der Bereich überhaupt sichtbar ist — sonst würde
              der Panel-eigene Ladeaufruf für Nicht-Admins unnötig gegen die
              admin-geschützte Route laufen und dort auf 403 laufen. */}
          {benutzer.isAdmin && (
            <div className={bereich === 'api-schluessel' ? '' : 'hidden'}>
              <ApiSchluesselPanel />
            </div>
          )}
        </div>
      </main>

      <footer className="px-4 sm:px-6 py-4 border-t border-gray-200 dark:border-gray-700 text-xs text-gray-400 dark:text-gray-500">
        <div className="max-w-5xl mx-auto">
          PTI Pseudonymisierer · Phase 1. Die Verarbeitung läuft lokal auf dem PTI-Server; es
          werden keine Inhalte an Dritte übermittelt.
        </div>
      </footer>
    </div>
  );
}
