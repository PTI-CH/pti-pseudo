/**
 * Das Auftragsgedächtnis ist eine Bequemlichkeit — und muss sich auch so
 * verhalten.
 *
 * Zwei Zusagen stehen hier auf dem Spiel. Erstens: Ein Eintrag, der auf einen
 * Auftrag zeigt, den der Server längst entfernt hat (älter als seine
 * Aufbewahrung von 24 Stunden), oder ein Eintrag, den irgendetwas verstümmelt
 * hat, wird verworfen statt benutzt — sonst begrüsste die Oberfläche den
 * Menschen bei jedem Start mit einer Nachfrage, die zuverlässig in einer 404
 * endet.
 *
 * Zweitens, und wichtiger: **Kein Zugriff darf eine Ausnahme nach aussen
 * lassen.** `localStorage` ist in einem privaten Fenster, bei blockierten
 * Website-Daten oder vollem Speicher nicht bloss leer — es wirft, teils schon
 * beim blossen Lesen der Eigenschaft. Eine Ausnahme von hier risse den
 * Wiederaufnahme-Effekt und damit das ganze Panel mit, und zwar bei jenen
 * Nutzern, die von der Wiederaufnahme ohnehin nichts hätten.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  merkeAuftrag,
  offenerAuftrag,
  raeumeAbgelaufenenAuftragAb,
  vergissAuftrag,
} from './auftragsspeicher';

/**
 * Spiegelt `SCHLUESSEL` aus dem Modul.
 *
 * Bewusst hier wiederholt statt exportiert: Nur der Test für den
 * verstümmelten Eintrag muss am Modul vorbei schreiben — ein Export nur für
 * einen Test machte den Schlüssel zu öffentlicher Fläche.
 */
const SCHLUESSEL = 'pti-pseudo.offener-auftrag';

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('merken, lesen, vergessen', () => {
  it('gibt einen gemerkten Auftrag unverändert wieder heraus', () => {
    merkeAuftrag({ id: 'auftrag-1', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    const offen = offenerAuftrag();
    expect(offen?.id).toBe('auftrag-1');
    expect(offen?.art).toBe('anonymisieren');
    expect(offen?.dateiname).toBe('grossakte.docx');
  });

  it('gibt nach dem Vergessen nichts mehr heraus', () => {
    merkeAuftrag({ id: 'auftrag-1', art: 'rueckfuehren', dateiname: 'akte.docx' });
    vergissAuftrag();

    expect(offenerAuftrag()).toBeNull();
    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });
});

describe('unbrauchbare Einträge', () => {
  it('verwirft einen Eintrag, der älter als die Aufbewahrung des Servers ist', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-25T08:00:00Z'));
    merkeAuftrag({ id: 'auftrag-alt', art: 'anonymisieren', dateiname: 'akte.docx' });

    // Eine Stunde jenseits der 24, die der Server aufbewahrt.
    vi.setSystemTime(new Date('2026-08-26T09:00:00Z'));

    expect(offenerAuftrag()).toBeNull();
    // Und er ist weg, nicht nur ignoriert: Ein Eintrag, den niemand entfernt,
    // machte die Prüfung bei jedem Seitenaufruf aufs Neue nötig.
    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });

  it('verwirft einen unlesbaren Eintrag, statt zu werfen', () => {
    localStorage.setItem(SCHLUESSEL, '{das ist kein JSON');

    expect(() => offenerAuftrag()).not.toThrow();
    expect(offenerAuftrag()).toBeNull();
    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });
});

describe('Trennung der beiden Auftragsarten', () => {
  it('gibt einen Eintrag der anderen Art nicht heraus und löscht ihn auch nicht', () => {
    merkeAuftrag({ id: 'auftrag-1', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    // Der Bereich „Rückführen“ fragt beim Mount ebenfalls nach — er darf den
    // Auftrag des anderen Bereichs weder aufnehmen noch wegräumen. Täte er
    // Letzteres, verlöre der Bereich „Pseudonymisieren“ seinen Auftrag
    // allein dadurch, dass jemand kurz die Ansicht gewechselt hat.
    expect(offenerAuftrag('rueckfuehren')).toBeNull();
    expect(offenerAuftrag('anonymisieren')?.id).toBe('auftrag-1');
  });

});

describe('Vergessen nach Kennung', () => {
  it('räumt den Eintrag eines zweiten Auftrags DERSELBEN Art nicht weg', () => {
    // Der Fall, den eine Prüfung nach Art nicht sieht: Ein grosser Lauf
    // startet, ein zweiter grosser Lauf desselben Bereichs überschreibt
    // seinen Eintrag, dann wird der erste fertig und räumt auf. Nach Art
    // passte er — und nähme den Eintrag des noch laufenden zweiten mit, der
    // damit nach einem Neuladen unauffindbar wäre.
    //
    // Dass die gesperrte Oberfläche das heute verhindert, trägt diese
    // Zusicherung nicht: Die Sperre liegt in einem anderen Modul und kann bei
    // der nächsten Änderung an den Bedienelementen wegfallen.
    merkeAuftrag({ id: 'auftrag-2', art: 'anonymisieren', dateiname: 'zweite.docx' });

    vergissAuftrag('auftrag-1');

    expect(offenerAuftrag('anonymisieren')?.id).toBe('auftrag-2');
  });

  it('räumt den Eintrag des anderen Bereichs nicht weg', () => {
    merkeAuftrag({ id: 'auftrag-r', art: 'rueckfuehren', dateiname: 'akte.docx' });

    vergissAuftrag('auftrag-a');

    expect(offenerAuftrag('rueckfuehren')?.id).toBe('auftrag-r');
    expect(localStorage.getItem(SCHLUESSEL)).not.toBeNull();
  });

  it('vergisst den eigenen Eintrag sehr wohl', () => {
    // Sonst wäre die Prüfung nur ein Verhindern und kein Unterscheiden.
    merkeAuftrag({ id: 'auftrag-r', art: 'rueckfuehren', dateiname: 'akte.docx' });

    vergissAuftrag('auftrag-r');

    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });

  it('räumt einen unlesbaren Eintrag auch mit Kennung weg', () => {
    // Ein verstümmelter Eintrag gehört niemandem. Bliebe er liegen,
    // blockierte er den einen Speicherplatz dauerhaft: `merkeAuftrag`
    // überschriebe ihn zwar, aber bis dahin fände `offenerAuftrag` nichts
    // und niemand räumte ihn ab.
    localStorage.setItem(SCHLUESSEL, '{das ist kein JSON');

    vergissAuftrag('auftrag-1');

    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });

  it('räumt einen Eintrag ohne Kennung weg', () => {
    // Lesbares JSON, aber ohne `id` — etwa aus einer älteren Fassung dieses
    // Moduls. Auch der gehört niemandem.
    localStorage.setItem(
      SCHLUESSEL,
      JSON.stringify({ art: 'anonymisieren', dateiname: 'akte.docx', angelegt: Date.now() }),
    );

    vergissAuftrag('auftrag-1');

    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });
});

/**
 * Die Alterungsgrenze darf nicht davon abhängen, dass jemand liest.
 *
 * Bis zum 2026-08-26 wirkte `HOECHSTALTER_MS` allein in `offenerAuftrag`, und
 * den ruft nur, wer einen der beiden Auftragsbereiche betritt. Wer das
 * Werkzeug schloss und den Bereich nie wieder öffnete, dessen **Dateiname**
 * lag unbegrenzt im Browserprofil — auf einem geteilten Arbeitsplatz die
 * Frage, die man stellen muss.
 */
describe('Abräumen ohne Leser', () => {
  it('entfernt einen abgelaufenen Eintrag, ohne dass jemand ihn liest', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-25T08:00:00Z'));
    merkeAuftrag({ id: 'auftrag-alt', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    vi.setSystemTime(new Date('2026-08-26T09:00:00Z'));
    raeumeAbgelaufenenAuftragAb();

    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });

  it('lässt einen noch gültigen Eintrag stehen', () => {
    // Das Abräumen läuft beim Start der Anwendung, also **vor** der
    // Wiederaufnahme. Nähme es einen laufenden Auftrag mit, zerstörte es
    // genau das, wofür es das Gedächtnis gibt.
    merkeAuftrag({ id: 'auftrag-1', art: 'anonymisieren', dateiname: 'grossakte.docx' });

    raeumeAbgelaufenenAuftragAb();

    expect(offenerAuftrag('anonymisieren')?.id).toBe('auftrag-1');
  });

  it('entfernt einen unlesbaren Eintrag', () => {
    localStorage.setItem(SCHLUESSEL, '{das ist kein JSON');

    expect(() => raeumeAbgelaufenenAuftragAb()).not.toThrow();
    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });
});

describe('feindlicher localStorage', () => {
  /** Ersetzt `window.localStorage` für die Dauer von `pruefung`. */
  function mitSpeicher(ersatz: PropertyDescriptor, pruefung: () => void): void {
    const vorher = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', { configurable: true, ...ersatz });
    try {
      pruefung();
    } finally {
      if (vorher) Object.defineProperty(window, 'localStorage', vorher);
      else delete (window as unknown as Record<string, unknown>).localStorage;
    }
  }

  it('bleibt still, wenn schon der Zugriff auf localStorage wirft', () => {
    // Der Fall eines Browsers mit blockierten Website-Daten: nicht „leer“,
    // sondern eine SecurityError schon beim Lesen der Eigenschaft.
    mitSpeicher(
      {
        get() {
          throw new Error('Zugriff auf Website-Daten ist gesperrt.');
        },
      },
      () => {
        expect(() =>
          merkeAuftrag({ id: 'a', art: 'anonymisieren', dateiname: 'x.docx' }),
        ).not.toThrow();
        expect(() => offenerAuftrag()).not.toThrow();
        expect(() => vergissAuftrag()).not.toThrow();
        expect(offenerAuftrag()).toBeNull();
      },
    );
  });

  it('bleibt still, wenn das Abräumen beim Start auf einen gesperrten Speicher trifft', () => {
    // `App.tsx` räumt beim Mount ab. Risse dieser Aufruf eine Ausnahme nach
    // aussen, bliebe die ganze Anwendung weiss — und zwar bei jenen Nutzern,
    // die vom Auftragsgedächtnis ohnehin nichts haben.
    mitSpeicher(
      {
        get() {
          throw new Error('Zugriff auf Website-Daten ist gesperrt.');
        },
      },
      () => {
        expect(() => raeumeAbgelaufenenAuftragAb()).not.toThrow();
      },
    );
  });

  it('bleibt still, wenn die Methoden des Speichers werfen', () => {
    // Der Fall eines vollen Speicherkontingents: Die Eigenschaft ist da, aber
    // jede Methode wirft. Ein `try`/`catch` nur um den Eigenschaftszugriff
    // reichte dafür nicht.
    const werfen = () => {
      throw new Error('QuotaExceededError');
    };
    mitSpeicher(
      { value: { getItem: werfen, setItem: werfen, removeItem: werfen } },
      () => {
        expect(() =>
          merkeAuftrag({ id: 'a', art: 'rueckfuehren', dateiname: 'x.docx' }),
        ).not.toThrow();
        expect(() => vergissAuftrag()).not.toThrow();
        expect(() => offenerAuftrag()).not.toThrow();
        expect(offenerAuftrag('rueckfuehren')).toBeNull();
      },
    );
  });
});
