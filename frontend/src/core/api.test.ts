/**
 * `warteAufAuftrag` fragt einen asynchronen Auftrag ab, bis er fertig ist.
 *
 * Zwei Dinge muss der Test zeigen: Erstens meldet er den Fortschritt jeder
 * Zwischenantwort weiter (die Oberfläche zeigt „Seite 3 von 10“ nur, wenn das
 * ankommt). Zweitens wirft er bei `status: 'fehler'` die Meldung des Servers
 * unverändert — sie ist für Menschen geschrieben, eine eigene Formulierung
 * würde sie verfälschen.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { warteAufAuftrag } from './api';

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ toFake: ['setTimeout'] });
});

describe('warteAufAuftrag', () => {
  it('fragt bis zum Ende nach und liefert dann das Ergebnis', async () => {
    const antworten = [
      { status: 'wartend', fortschritt: null, gesamt: null },
      { status: 'laeuft', fortschritt: 3, gesamt: 10 },
      { status: 'fertig', fortschritt: 10, gesamt: 10 },
    ];
    const stufen: number[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/result')) {
        // Die Ergebnisroute liefert seit Aufgabe 9 dieselbe Form wie der
        // synchrone Pfad — `filename`, nicht mehr `dateiname`.
        return new Response(JSON.stringify({ filename: 'a.txt', content_base64: 'eA==' }));
      }
      return new Response(JSON.stringify(antworten.shift()));
    });

    const lauf = warteAufAuftrag('abc', (f) => stufen.push(f ?? 0));
    await vi.runAllTimersAsync();
    const ergebnis = await lauf;

    expect(stufen).toContain(3);
    expect(ergebnis.dateiname).toBe('a.txt');
  });

  it('wirft mit der Meldung des Servers, wenn der Auftrag scheitert', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'fehler', fehler: 'Format nicht unterstützt' })),
    );
    await expect(warteAufAuftrag('abc', () => {})).rejects.toThrow('Format nicht unterstützt');
  });
});
