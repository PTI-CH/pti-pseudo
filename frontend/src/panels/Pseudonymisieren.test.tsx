/**
 * Der Befund stammt aus dem Lauf — die Ersetzung muss zu ihm passen.
 *
 * Die Bedienelemente bleiben nach einem Lauf bedienbar: Modus, Kategorien und
 * die Quelle Text/Datei lassen sich weiter umstellen, während die Fundliste
 * darunter noch die Ergebnisse des alten Laufs zeigt. „Anwenden“ schickt
 * deshalb den Stand des **Laufs**, nicht den der Bedienelemente.
 *
 * Beim Modus wiegt das am schwersten: Ein Wechsel von „umkehrbar“ auf
 * „anonym“ nach dem Lauf erzeugte sonst eine Ersetzung ohne Tresor. Die
 * geprüfte Fundliste sähe unverändert aus, aber die Rückführung wäre
 * unmöglich geworden, ohne dass es jemand angeordnet hätte.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import Pseudonymisieren from './Pseudonymisieren';
import { anonymisiereText, ladeKategorien } from '../core/api';

// Nur die Netzwerkfunktionen werden ersetzt. `fehlerMeldung` und
// `gibErgebnisFrei` bleiben echt — sie sind reine Logik, und ein Mock davon
// würde nur den Test bestätigen statt das Panel.
vi.mock('../core/api', async (original) => {
  const echt = await original<typeof import('../core/api')>();
  return {
    ...echt,
    ladeKategorien: vi.fn(),
    anonymisiereText: vi.fn(),
    anonymisiereDatei: vi.fn(),
  };
});

const ERGEBNIS = {
  text: 'Es schrieb [[PERSON_1]].',
  befunde: [
    {
      id: 'text-PERSON-9-0',
      segment_id: 'text',
      entity_type: 'PERSON',
      text: 'Andrea Beispiel',
      start: 9,
      end: 24,
      score: 0.85,
      propagated: false,
    },
  ],
  angewendet: null,
  vaultId: '11111111-2222-3333-4444-555555555555',
  downloadUrl: null,
  dateiname: null,
};

beforeEach(() => {
  vi.mocked(ladeKategorien).mockResolvedValue([
    { entity_type: 'PERSON', label: 'Person', default_policy: 'redact', description: '' },
  ] as never);
  vi.mocked(anonymisiereText).mockResolvedValue(ERGEBNIS as never);
});

/** Führt einen Lauf im Modus „umkehrbar“ aus und wartet auf die Fundliste. */
async function laufImUmkehrbarenModus(nutzer: ReturnType<typeof userEvent.setup>) {
  render(<Pseudonymisieren />);
  await screen.findByText('Person');

  await nutzer.type(screen.getByRole('textbox'), 'Es schrieb Andrea Beispiel.');
  await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

  await screen.findByRole('button', { name: 'Anwenden' });
}

describe('Anwenden nach einem Lauf', () => {
  it('schickt den Modus des Laufs, nicht den aktuell gewählten', async () => {
    const nutzer = userEvent.setup();
    await laufImUmkehrbarenModus(nutzer);

    expect(vi.mocked(anonymisiereText).mock.calls[0][1]).toBe('umkehrbar');

    // Der Mensch schaltet nach dem Lauf um — die Fundliste zeigt weiterhin
    // die Funde des umkehrbaren Laufs.
    await nutzer.click(screen.getByRole('button', { name: /Anonym/ }));
    await nutzer.click(screen.getByRole('button', { name: 'Anwenden' }));

    await waitFor(() => expect(vi.mocked(anonymisiereText)).toHaveBeenCalledTimes(2));
    expect(vi.mocked(anonymisiereText).mock.calls[1][1]).toBe('umkehrbar');
  });

  it('beschriftet das Ergebnis nach dem Modus des Laufs', async () => {
    const nutzer = userEvent.setup();
    await laufImUmkehrbarenModus(nutzer);

    await nutzer.click(screen.getByRole('button', { name: /Anonym/ }));

    // Der Lauf war umkehrbar und hat einen Tresor angelegt. Die Fussnote darf
    // nicht behaupten, es sei keine Zuordnung gespeichert worden — die
    // Tresor-Kennung steht direkt darueber.
    expect(screen.queryByText(/keine Zuordnung gespeichert/)).not.toBeInTheDocument();
    expect(screen.getByText(/11111111-2222-3333-4444-555555555555/)).toBeInTheDocument();
  });
});
