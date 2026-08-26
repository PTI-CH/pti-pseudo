/**
 * Auch die Rückführung kann zu einem Auftrag werden — und war nach einem
 * Seitenneuladen ebenso verloren.
 *
 * Hier wiegt der Verlust sogar schwerer als beim Pseudonymisieren: Ein
 * zweiter Anlauf verlangt dieselbe grosse Datei **und** die Kennung der
 * Zuordnungstabelle noch einmal.
 *
 * Bewusst kein Modul-Mock von `../core/api`: Ersetzt ist allein `fetch`.
 * Damit läuft alles echt, was die Zusage trägt — `offenerAuftrag`,
 * `verfolgeAuftrag`, `warteAufAuftrag` und die Antwort-Normalisierung.
 * **In keinem Test dieser Datei klickt jemand.**
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import Rueckfuehren from './Rueckfuehren';
import { merkeAuftrag } from '../core/auftragsspeicher';

const VAULT_ID = '99999999-8888-7777-6666-555555555555';

/** Alle Pfade, die das Panel angefragt hat — je Test frisch. */
let gefragt: string[] = [];

beforeEach(() => {
  localStorage.clear();
  gefragt = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

/**
 * Beantwortet die Vault-Liste und, falls gefragt, einen fertigen Auftrag.
 *
 * Der Auftrag antwortet beim ersten Blick mit `fertig` — die Wartezeit
 * gehört zu `warteAufAuftrag` und ist dort geprüft (`core/api.test.ts`);
 * dieser Test fragt, ob das Panel von selbst nachfragt.
 */
function antworteMitFertigemAuftrag(): void {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (eingabe) => {
    const url = String(eingabe);
    gefragt.push(url);
    if (url.startsWith('/api/v1/mappings')) {
      return new Response(
        JSON.stringify({ mappings: [{ id: VAULT_ID, document_name: 'Fallakte', entry_count: 3 }] }),
      );
    }
    if (url.endsWith('/result')) {
      // Der Umschlag des Auftrags trägt die `vault_id` selbst mit
      // (`backend/routers/jobs.py:ergebnis`).
      return new Response(
        JSON.stringify({
          filename: 'akte.docx',
          content_base64: 'eA==',
          vault_id: VAULT_ID,
        }),
      );
    }
    return new Response(JSON.stringify({ status: 'fertig', fortschritt: null, gesamt: null }));
  });
}

describe('Wiederaufnahme einer Rückführung nach einem Seitenneuladen', () => {
  it('fragt den gemerkten Auftrag von selbst ab und zeigt sein Ergebnis', async () => {
    merkeAuftrag({ id: 'rf-1', art: 'rueckfuehren', dateiname: 'akte.docx' });
    antworteMitFertigemAuftrag();

    render(<Rueckfuehren />);

    expect(await screen.findByText(/akte.docx herunterladen/)).toBeInTheDocument();
    expect(gefragt).toContain('/api/v1/jobs/rf-1');
    expect(gefragt).toContain('/api/v1/jobs/rf-1/result');
  });

  it('setzt die Zuordnungstabelle des Laufs wieder ein', async () => {
    merkeAuftrag({ id: 'rf-1', art: 'rueckfuehren', dateiname: 'akte.docx' });
    antworteMitFertigemAuftrag();

    render(<Rueckfuehren />);
    await screen.findByText(/akte.docx herunterladen/);

    // Die gewählte `vault_id` ist nach dem Neuladen nicht mehr im Panel. Für
    // das Ergebnis schadet das nicht — der Server hat sie in `parameter_json`
    // festgehalten und gibt sie mit dem Umschlag zurück. Wir setzen sie
    // trotzdem zurück in die Auswahl, damit ein nächster Lauf nicht erneut
    // danach fragt.
    expect(screen.getByLabelText(/Zuordnungstabelle/)).toHaveValue(VAULT_ID);
  });

  it('nimmt einen Auftrag der anderen Art nicht auf', async () => {
    // Derselbe Speicherplatz trägt beide Arten. Griffe dieses Panel nach
    // einem Auftrag des Bereichs „Pseudonymisieren“, führte es das falsche
    // Ergebnis vor — und der andere Bereich fände seinen Auftrag nie wieder.
    merkeAuftrag({ id: 'auftrag-1', art: 'anonymisieren', dateiname: 'grossakte.docx' });
    antworteMitFertigemAuftrag();

    render(<Rueckfuehren />);
    await screen.findByRole('option', { name: /Fallakte/ });

    expect(gefragt.some((u) => u.includes('/jobs/'))).toBe(false);
    expect(screen.queryByText(/Ein Auftrag von vorhin/)).not.toBeInTheDocument();
  });
});
