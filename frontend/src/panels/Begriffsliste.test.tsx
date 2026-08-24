/**
 * Lesen darf jeder, ändern nur Administratoren.
 *
 * Das Backend setzt das durch (`403` in `routers/terms.py`) — die Oberfläche
 * muss es *zeigen*. Ein „Löschen"-Knopf, der zuverlässig einen Fehler erzeugt,
 * sieht aus wie eine kaputte Anwendung und nicht wie eine fehlende
 * Berechtigung. Genau das bekamen Kolleginnen und Kollegen ohne Admin-Rolle
 * zu sehen, bevor dieser Test entstand.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import Begriffsliste from './Begriffsliste';
import { ladeBegriffe } from '../core/api';

vi.mock('../core/api', async (original) => {
  const echt = await original<typeof import('../core/api')>();
  return { ...echt, ladeBegriffe: vi.fn(), legeBegriffAn: vi.fn(), loescheBegriff: vi.fn() };
});

const BEGRIFFE = [
  { id: 'b1', begriff: 'Meldestelle für Geldwäscherei', typ: 'ORGANIZATION', ersatz: '' },
];

beforeEach(() => {
  vi.mocked(ladeBegriffe).mockResolvedValue(BEGRIFFE as never);
});

describe('Begriffsliste ohne Admin-Rolle', () => {
  it('zeigt die Begriffe, aber kein Anlegen und kein Löschen', async () => {
    render(<Begriffsliste istAdmin={false} />);

    // Lesen ist erlaubt und muss sichtbar bleiben — die Liste erklärt, warum
    // ein Dokument so pseudonymisiert wurde, wie es wurde.
    expect(await screen.findByText('Meldestelle für Geldwäscherei')).toBeInTheDocument();

    expect(screen.queryByRole('button', { name: /Hinzufügen/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Löschen' })).not.toBeInTheDocument();
  });

  it('sagt, wer die Liste pflegt', async () => {
    render(<Begriffsliste istAdmin={false} />);
    await screen.findByText('Meldestelle für Geldwäscherei');
    expect(screen.getByText(/Rolle „admin“/)).toBeInTheDocument();
  });
});

describe('Begriffsliste als Admin', () => {
  it('bietet Anlegen und Löschen an', async () => {
    render(<Begriffsliste istAdmin />);
    await screen.findByText('Meldestelle für Geldwäscherei');

    expect(screen.getByRole('button', { name: /Hinzufügen/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Löschen' })).toBeInTheDocument();
  });
});
