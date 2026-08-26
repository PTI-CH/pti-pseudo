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
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import Pseudonymisieren from './Pseudonymisieren';
import { anonymisiereDatei, anonymisiereText, ladeKategorien } from '../core/api';
import { merkeAuftrag, offenerAuftrag } from '../core/auftragsspeicher';

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
  kiGeprueft: true,
};

beforeEach(() => {
  // Ohne das summieren sich die Aufrufe ueber Tests hinweg, und `calls[0]`
  // stammt dann aus einem frueheren Test statt aus diesem.
  vi.clearAllMocks();
  // Ein Eintrag aus einem früheren Test löste beim Mount eine Wiederaufnahme
  // aus — mit gesperrten Bedienelementen und einem Netzaufruf, mit dem der
  // betreffende Test nichts zu tun hätte.
  localStorage.clear();
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


describe('KI-Pruefung', () => {
  it('ist vorangehakt und wird mitgeschickt', async () => {
    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');

    const feld = screen.getByRole('checkbox', { name: /KI-Prüfung/ });
    expect(feld).toBeChecked();

    await nutzer.type(screen.getByRole('textbox'), 'Es schrieb Andrea Beispiel.');
    await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

    await waitFor(() => expect(vi.mocked(anonymisiereText)).toHaveBeenCalled());
    expect(vi.mocked(anonymisiereText).mock.calls[0][3]).toBe(true);
  });

  it('schickt false, wenn das Haekchen weg ist', async () => {
    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');

    await nutzer.click(screen.getByRole('checkbox', { name: /KI-Prüfung/ }));
    await nutzer.type(screen.getByRole('textbox'), 'Text');
    await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

    await waitFor(() => expect(vi.mocked(anonymisiereText)).toHaveBeenCalled());
    expect(vi.mocked(anonymisiereText).mock.calls[0][3]).toBe(false);
  });

  it('warnt, wenn die Pruefung angefordert war, aber nicht lief', async () => {
    vi.mocked(anonymisiereText).mockResolvedValue({
      ...ERGEBNIS,
      kiGeprueft: false,
    } as never);
    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');
    await nutzer.type(screen.getByRole('textbox'), 'Text');
    await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

    expect(await screen.findByText(/nur lokal geprüft/)).toBeInTheDocument();
  });

  it('warnt NICHT, wenn die Pruefung gar nicht angefordert war', async () => {
    vi.mocked(anonymisiereText).mockResolvedValue({
      ...ERGEBNIS,
      kiGeprueft: false,
    } as never);
    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');

    await nutzer.click(screen.getByRole('checkbox', { name: /KI-Prüfung/ }));
    await nutzer.type(screen.getByRole('textbox'), 'Text');
    await nutzer.click(screen.getByRole('button', { name: 'Pseudonymisieren' }));

    await screen.findByRole('button', { name: 'Anwenden' });
    expect(screen.queryByText(/nur lokal geprüft/)).not.toBeInTheDocument();
  });
});


/**
 * Ein laufender Auftrag war nach einem Seitenneuladen weg.
 *
 * Die `job_id` lebte nur im Speicher der Seite. Der Auftrag lief weiter und
 * war 24 Stunden abholbar, aber es gibt keine Liste eigener Aufträge, nur
 * `GET /jobs/{id}` — er war damit unauffindbar, und der Mensch begann
 * dieselbe grosse Datei von vorn.
 *
 * Geprüft wird die Zusage, nicht der Speicher: Liegt beim Mount ein Eintrag,
 * fragt die Oberfläche von selbst nach und zeigt das Ergebnis. **In diesen
 * beiden Tests klickt niemand** — es gibt bewusst kein `userEvent`.
 *
 * `verfolgeAuftrag` und `warteAufAuftrag` bleiben echt (der Modul-Mock oben
 * ersetzt nur die drei genannten Funktionen); ersetzt ist allein `fetch`.
 * Ein Mock der Auftragsverfolgung hätte den Weg geprüft, den der Test selbst
 * gebaut hat.
 */
describe('Wiederaufnahme eines Auftrags nach einem Seitenneuladen', () => {
  afterEach(() => {
    // `clearAllMocks` setzt Spione nicht zurück — ohne das hier bekäme der
    // nächste Test in dieser Datei unser `fetch` statt seines eigenen.
    vi.restoreAllMocks();
  });

  it('fragt den gemerkten Auftrag von selbst ab und zeigt sein Ergebnis', async () => {
    merkeAuftrag({ id: 'auftrag-1', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    const gefragt: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (eingabe) => {
      const url = String(eingabe);
      gefragt.push(url);
      if (url.endsWith('/result')) {
        // Dieselbe Form wie der synchrone Pfad — `backend/routers/jobs.py`
        // packt dafür den verschlüsselten Umschlag aus.
        return new Response(
          JSON.stringify({
            filename: 'grossakte.docx',
            content_base64: 'eA==',
            entities: [
              {
                segment_id: 'body/p0',
                entity_type: 'PERSON',
                text: 'Andrea Beispiel',
                start: 0,
                end: 15,
                score: 0.9,
              },
            ],
            applied: 0,
            vault_id: '99999999-8888-7777-6666-555555555555',
          }),
        );
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });

    render(<Pseudonymisieren />);

    expect(await screen.findByText('Andrea Beispiel')).toBeInTheDocument();
    expect(gefragt).toContain('/api/v1/jobs/auftrag-1');
    expect(gefragt).toContain('/api/v1/jobs/auftrag-1/result');
    // Die Tresor-Kennung kommt aus dem Auftragsergebnis. Ohne sie wäre das
    // umkehrbar verarbeitete Dokument für immer unrückführbar.
    expect(
      screen.getByText(/99999999-8888-7777-6666-555555555555/),
    ).toBeInTheDocument();
  });

  it('nennt die Datei, die noch läuft', async () => {
    merkeAuftrag({ id: 'auftrag-2', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    let aufrufe = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      aufrufe += 1;
      if (aufrufe === 1) {
        return Promise.resolve(
          new Response(JSON.stringify({ status: 'laeuft', fortschritt: 3, gesamt: 10 })),
        );
      }
      // Weitere Nachfragen bleiben offen: Dieser Test will den Wartezustand
      // sehen, nicht das Ende.
      return new Promise<Response>(() => {});
    });

    render(<Pseudonymisieren />);

    // Ohne den Dateinamen wirkt der Fortschrittshinweis nach einem Neuladen
    // wie ein Überbleibsel — die Datei selbst ist ja weg —, und der Mensch
    // lädt dieselbe grosse Datei ein zweites Mal hoch.
    expect(await screen.findByText('grossakte.docx')).toBeInTheDocument();
    expect(
      screen.queryByText(/Die Datei wird im Hintergrund verarbeitet/),
    ).not.toBeInTheDocument();
  });
});

/**
 * „Anwenden“ nach einer Wiederaufnahme.
 *
 * Der Wiederaufnahme-Effekt hat `[]`-Deps und liest damit die Closure des
 * Mounts. Dort ist die Kategorienwahl noch leer (der Katalog lädt erst
 * danach) und der Modus steht auf dem Vorgabewert `umkehrbar`. Übernahm die
 * Wiederaufnahme diese Werte als Stand des Laufs, war „Anwenden“ zweimal
 * falsch: Es brach mit „Wählen Sie mindestens eine Kategorie aus“ ab, obwohl
 * in der Oberfläche eine angehakt ist — der Mensch konnte also bei genau den
 * grossen Dokumenten keinen Fehlalarm abwählen, für die die Fundliste im
 * Auftragsergebnis überhaupt mitgeschickt wird. Und wer einen `anonym`-Lauf
 * wiederaufnahm, legte mit „Anwenden“ einen Tresor an, den er nicht bestellt
 * hatte: Klarnamen in einer Zuordnungstabelle.
 *
 * Die Werte müssen deshalb aus dem Auftragsergebnis kommen
 * (`backend/jobs/ausfuehrung.py` legt sie hinein), nicht aus der Closure.
 */
describe('Anwenden nach einer Wiederaufnahme', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** Antwort der Ergebnisroute; `umschlag` überschreibt/ergänzt die Felder. */
  function fetchMitUmschlag(umschlag: Record<string, unknown>) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (eingabe) => {
      if (String(eingabe).endsWith('/result')) {
        return new Response(
          JSON.stringify({
            filename: 'grossakte.docx',
            content_base64: 'eA==',
            entities: [
              {
                segment_id: 'body/p0',
                entity_type: 'PERSON',
                text: 'Andrea Beispiel',
                start: 0,
                end: 15,
                score: 0.9,
              },
            ],
            applied: 1,
            ...umschlag,
          }),
        );
      }
      return new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 }));
    });
  }

  /**
   * Nimmt den Auftrag auf und legt die Datei erneut vor.
   *
   * Nach einem Neuladen ist die Datei weg (ein `File` überlebt es nicht) —
   * „Anwenden“ verlangt sie zu Recht erneut. Erst danach kommt der Lauf
   * überhaupt bis zu Modus und Kategorien.
   */
  async function wiederaufnehmenUndDateiWaehlen(
    nutzer: ReturnType<typeof userEvent.setup>,
  ) {
    render(<Pseudonymisieren />);
    await screen.findByRole('button', { name: 'Anwenden' });

    await nutzer.click(screen.getByRole('button', { name: 'Datei' }));
    const datei = new File(['Andrea Beispiel'], 'grossakte.docx');
    await nutzer.upload(document.querySelector('input[type="file"]') as HTMLInputElement, datei);
    await screen.findByText('grossakte.docx');
  }

  it('schickt Modus und Kategorien des Auftrags, nicht die Vorgabewerte', async () => {
    merkeAuftrag({ id: 'auftrag-3', art: 'anonymisieren', dateiname: 'grossakte.docx' });
    fetchMitUmschlag({
      mode: 'anonymous',
      requested_entities: ['PERSON', 'CH_AHV'],
      ki_pruefung: false,
    });
    vi.mocked(anonymisiereDatei).mockResolvedValue(ERGEBNIS as never);

    const nutzer = userEvent.setup();
    await wiederaufnehmenUndDateiWaehlen(nutzer);
    await nutzer.click(screen.getByRole('button', { name: 'Anwenden' }));

    await waitFor(() => expect(vi.mocked(anonymisiereDatei)).toHaveBeenCalled());
    const aufruf = vi.mocked(anonymisiereDatei).mock.calls[0];
    // Der Lauf war `anonym`. `umkehrbar` legte hier einen Tresor an, den
    // niemand bestellt hat — der schwerste der drei Fehler.
    expect(aufruf[1]).toBe('anonym');
    expect(aufruf[2]).toEqual(['PERSON', 'CH_AHV']);
    expect(aufruf[3]).toBe(false);
  });

  it('verweigert Anwenden bei einem Auftrag ohne überlieferte Einstellungen', async () => {
    merkeAuftrag({ id: 'auftrag-4', art: 'anonymisieren', dateiname: 'grossakte.docx' });
    // Ein Umschlag aus der Zeit vor diesen Feldern — nach einem Deploy bis zu
    // 24 Stunden lang real möglich. Geraten wird nichts: `umkehrbar` wäre
    // wieder der unbestellte Tresor, und eine leere Kategorienliste ergäbe
    // eine Fehlermeldung, die der Oberfläche widerspricht.
    fetchMitUmschlag({ vault_id: null });
    vi.mocked(anonymisiereDatei).mockResolvedValue(ERGEBNIS as never);

    const nutzer = userEvent.setup();
    await wiederaufnehmenUndDateiWaehlen(nutzer);
    await nutzer.click(screen.getByRole('button', { name: 'Anwenden' }));

    expect(await screen.findByText(/älteren Fassung des Werkzeugs/)).toBeInTheDocument();
    expect(vi.mocked(anonymisiereDatei)).not.toHaveBeenCalled();
  });
});

/**
 * „Zurücksetzen“ ist die zweite Stelle, die vergisst.
 *
 * Beide Bereiche sind gleichzeitig montiert (`App.tsx`) und teilen sich einen
 * Speicherplatz. Räumte „Zurücksetzen“ hier bedingungslos ab, verlöre der
 * Bereich „Rückführen“ seinen noch laufenden Auftrag allein dadurch, dass
 * jemand nebenan aufgeräumt hat — und nach einem Neuladen wäre er
 * unauffindbar.
 */
describe('Zurücksetzen und das Auftragsgedächtnis', () => {
  it('lässt den Auftrag des Bereichs „Rückführen“ stehen', async () => {
    merkeAuftrag({ id: 'auftrag-r', art: 'rueckfuehren', dateiname: 'akte.docx' });

    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');

    await nutzer.click(screen.getByRole('button', { name: 'Zurücksetzen' }));

    expect(offenerAuftrag('rueckfuehren')?.id).toBe('auftrag-r');
  });

  it('vergisst den eigenen Auftrag trotzdem', async () => {
    // Sonst begrüsste der nächste Seitenaufruf den Menschen mit genau dem
    // Lauf, den er gerade verworfen hat.
    merkeAuftrag({ id: 'auftrag-a', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    // Der Wiederaufnahme-Effekt greift beim Mount und fragt nach — die
    // Antwort ist hier gleichgültig, sie muss nur ankommen.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (eingabe) =>
      String(eingabe).endsWith('/result')
        ? new Response(JSON.stringify({ filename: 'grossakte.docx', entities: [] }))
        : new Response(JSON.stringify({ status: 'fertig', fortschritt: 1, gesamt: 1 })),
    );

    const nutzer = userEvent.setup();
    render(<Pseudonymisieren />);
    await screen.findByText('Person');
    await waitFor(() => expect(offenerAuftrag('anonymisieren')).toBeNull());

    // Und auch ein Eintrag, den der Mensch danach von Hand hinterlässt (ein
    // zweiter Lauf), verschwindet beim Zurücksetzen.
    merkeAuftrag({ id: 'auftrag-a2', art: 'anonymisieren', dateiname: 'grossakte.docx' });
    await nutzer.click(screen.getByRole('button', { name: 'Zurücksetzen' }));

    expect(offenerAuftrag('anonymisieren')).toBeNull();
    vi.restoreAllMocks();
  });
});
