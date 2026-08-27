/**
 * Das Auftragsgedächtnis wird beim **Start der Anwendung** abgeräumt.
 *
 * Im gemerkten Eintrag steht ein Dateiname, und der kann in diesem Umfeld ein
 * Personendatum sein („Einvernahme_…docx"). Seine Alterungsgrenze hing bis zum
 * 2026-08-26 allein an `offenerAuftrag` — der wird nur aus den beiden
 * Auftragsbereichen gerufen. `App.tsx` ruft das Abräumen deshalb selbst,
 * unabhängig davon, welchen Reiter der Mensch anklickt.
 *
 * **Geprüft wird die Verdrahtung, nicht das Abräumen.** Was
 * `raeumeAbgelaufenenAuftragAb` tut, steht in `core/auftragsspeicher.test.ts`.
 * Hier zählt einzig, dass `App` es ruft — und das lässt sich am Verhalten
 * nicht ablesen: Beide Auftragsbereiche sind dauerhaft montiert und räumen
 * einen abgelaufenen Eintrag beim Lesen ohnehin mit weg. Ein Test auf „der
 * Eintrag ist danach fort" bliebe deshalb auch dann grün, wenn der Aufruf in
 * `App.tsx` fehlte, und bewachte gar nichts. Genau diese Nebenwirkung ist der
 * Grund für den eigenen Aufruf: Würde ein Bereich eines Tages erst bei Bedarf
 * montiert — „API-Schlüssel" ist es bereits —, verschwände die
 * Aufbewahrungsgrenze still mit ihm.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';

import App from './App';
import { raeumeAbgelaufenenAuftragAb } from './core/auftragsspeicher';

// Nur diese eine Funktion wird beobachtet; alles andere bleibt echt, damit die
// mitmontierten Bereiche sich normal verhalten.
vi.mock('./core/auftragsspeicher', async (importOriginal) => {
  const echt = await importOriginal<typeof import('./core/auftragsspeicher')>();
  return { ...echt, raeumeAbgelaufenenAuftragAb: vi.fn(echt.raeumeAbgelaufenenAuftragAb) };
});

beforeEach(() => {
  localStorage.clear();
  vi.mocked(raeumeAbgelaufenenAuftragAb).mockClear();
  // Kein Netzverkehr aus den mitmontierten Bereichen. Die Antworten sind leer
  // und trotzdem gültig — dieser Test fragt nichts von ihnen ab.
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (eingabe) => {
    const url = String(eingabe);
    if (url.startsWith('/api/me')) {
      return new Response(JSON.stringify({ username: 'testnutzer', role: 'standard' }));
    }
    if (url.startsWith('/api/v1/entities')) return new Response(JSON.stringify({ entities: [] }));
    if (url.startsWith('/api/v1/mappings')) return new Response(JSON.stringify({ mappings: [] }));
    if (url.startsWith('/api/v1/terms')) return new Response(JSON.stringify({ terms: [] }));
    return new Response(JSON.stringify({}));
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Start der Anwendung', () => {
  it('räumt das Auftragsgedächtnis ab, ohne dass jemand einen Bereich wechselt', async () => {
    // `act` umschliesst das Rendern, weil die mitmontierten Bereiche beim
    // Mount laden und ihre Antworten sonst nach dem Testende einträfen —
    // eine React-Warnung, die mit dieser Zusicherung nichts zu tun hat.
    await act(async () => {
      render(<App />);
    });

    expect(vi.mocked(raeumeAbgelaufenenAuftragAb)).toHaveBeenCalled();
  });
});
