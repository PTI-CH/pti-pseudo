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

import { verfolgeAuftrag, warteAufAuftrag } from './api';
import { merkeAuftrag, offenerAuftrag } from './auftragsspeicher';

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ toFake: ['setTimeout'] });
  localStorage.clear();
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

/**
 * Zwei Bereiche, ein Speicherplatz.
 *
 * `App.tsx` hält „Pseudonymisieren“ und „Rückführen“ gleichzeitig montiert,
 * das Auftragsgedächtnis hat aber nur einen Platz. Wird der eine Lauf fertig,
 * während im anderen Bereich einer läuft, darf sein Aufräumen den fremden
 * Eintrag nicht mitnehmen — sonst ist die noch laufende Verarbeitung nach
 * einem Neuladen unauffindbar, und das ist genau der Verlust, gegen den das
 * Auftragsgedächtnis gebaut wurde.
 */
describe('verfolgeAuftrag räumt nur den eigenen Eintrag ab', () => {
  it('lässt den Auftrag des anderen Bereichs stehen', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/result')) {
        return new Response(JSON.stringify({ filename: 'gross.docx', content_base64: 'eA==' }));
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });

    const lauf = verfolgeAuftrag('auftrag-a', 'anonymisieren', 'gross.docx', () => {});
    // Während er läuft, startet der Bereich „Rückführen“ seinen eigenen
    // grossen Lauf. Sein Eintrag überschreibt den ersten — ein Platz.
    merkeAuftrag({ id: 'auftrag-r', art: 'rueckfuehren', dateiname: 'akte.docx' });

    await vi.runAllTimersAsync();
    await lauf;

    expect(offenerAuftrag('rueckfuehren')?.id).toBe('auftrag-r');
  });

  it('lässt auch einen zweiten Auftrag DERSELBEN Art stehen', async () => {
    // Die Prüfung nach Art deckte diesen Fall nicht ab: Zwei grosse Läufe im
    // selben Bereich, der zweite überschreibt den Eintrag des ersten, der
    // erste wird fertig — und räumte den fremden Eintrag weg, weil die Art
    // passt. Dass die während eines Laufs gesperrte Oberfläche das heute
    // verhindert, ist eine Absicherung in einem anderen Modul; sie kann bei
    // der nächsten Änderung an den Bedienelementen wegfallen, ohne dass
    // jemand an das Auftragsgedächtnis denkt.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/result')) {
        return new Response(JSON.stringify({ filename: 'gross.docx', content_base64: 'eA==' }));
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });

    const lauf = verfolgeAuftrag('auftrag-a1', 'anonymisieren', 'erste.docx', () => {});
    merkeAuftrag({ id: 'auftrag-a2', art: 'anonymisieren', dateiname: 'zweite.docx' });

    await vi.runAllTimersAsync();
    await lauf;

    expect(offenerAuftrag('anonymisieren')?.id).toBe('auftrag-a2');
  });

  it('vergisst den eigenen Eintrag weiterhin', async () => {
    // Die Gegenrichtung: Wäre das Vergessen ganz entfallen, begrüsste der
    // erledigte Auftrag den Menschen bei jedem Seitenstart aufs Neue — mit
    // einer Nachfrage, die in einer 404 endet, sobald der Server ihn
    // aufgeräumt hat.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/result')) {
        return new Response(JSON.stringify({ filename: 'gross.docx', content_base64: 'eA==' }));
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });

    const lauf = verfolgeAuftrag('auftrag-a', 'anonymisieren', 'gross.docx', () => {});
    await vi.runAllTimersAsync();
    await lauf;

    expect(offenerAuftrag('anonymisieren')).toBeNull();
  });
});

/**
 * Der Auftragsumschlag trägt die Einstellungen des Laufs, die synchrone
 * Antwort nicht — er ist eine Obermenge (siehe API.md). Ohne diese
 * Abbildung käme die Wiederaufnahme im Panel nie an sie heran.
 */
describe('normalisiereAntwort: die Einstellungen des Laufs', () => {
  it('liest Kategorien, Modus und KI-Häkchen aus dem Auftragsergebnis', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/result')) {
        return new Response(
          JSON.stringify({
            filename: 'gross.docx',
            content_base64: 'eA==',
            // Die Fundliste. Sie steht unter `entities` — die GESUCHTEN
            // Kategorien deshalb unter einem eigenen Schlüssel.
            entities: [],
            mode: 'anonymous',
            requested_entities: ['PERSON', 'CH_AHV'],
            ki_pruefung: true,
          }),
        );
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });

    const lauf = warteAufAuftrag('abc', () => {});
    await vi.runAllTimersAsync();
    const ergebnis = await lauf;

    expect(ergebnis.laufModus).toBe('anonym');
    expect(ergebnis.laufKategorien).toEqual(['PERSON', 'CH_AHV']);
    expect(ergebnis.laufKiPruefung).toBe(true);
  });

  it('macht aus fehlenden Feldern `null`, nicht die Vorgabewerte', async () => {
    // Ein Auftrag aus der Zeit vor diesen Feldern — nach einem Deploy bis zu
    // 24 Stunden lang real möglich. `umkehrbar` hier zu raten hiesse, für
    // einen `anonym`-Lauf beim nächsten Schritt einen Tresor anzulegen, den
    // niemand bestellt hat.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/result')) {
        return new Response(
          JSON.stringify({ filename: 'gross.docx', content_base64: 'eA==', entities: [] }),
        );
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });

    const lauf = warteAufAuftrag('abc', () => {});
    await vi.runAllTimersAsync();
    const ergebnis = await lauf;

    expect(ergebnis.laufModus).toBeNull();
    expect(ergebnis.laufKategorien).toBeNull();
    expect(ergebnis.laufKiPruefung).toBeNull();
  });
});
