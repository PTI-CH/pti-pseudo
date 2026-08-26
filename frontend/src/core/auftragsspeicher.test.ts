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

import { merkeAuftrag, offenerAuftrag, vergissAuftrag } from './auftragsspeicher';

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

  it('räumt beim Vergessen den Eintrag der anderen Art nicht weg', () => {
    merkeAuftrag({ id: 'auftrag-r', art: 'rueckfuehren', dateiname: 'akte.docx' });

    // Die andere Hälfte derselben Regel — bisher galt sie nur beim Lesen.
    // Beide Bereiche sind gleichzeitig montiert (`App.tsx`) und teilen sich
    // diesen einen Speicherplatz: Ein grosser Anonymisierungslauf, der
    // fertig wird, räumt am Ende auf. Ohne die Prüfung löschte er dabei den
    // Eintrag der Rückführung, die daneben noch läuft — und die wäre nach
    // einem Neuladen unauffindbar. Genau der Verlust, gegen den dieses Modul
    // gebaut wurde, nur von der anderen Seite.
    vergissAuftrag('anonymisieren');

    expect(offenerAuftrag('rueckfuehren')?.id).toBe('auftrag-r');
    expect(localStorage.getItem(SCHLUESSEL)).not.toBeNull();

    // Der eigene darf sehr wohl weg — sonst wäre die Prüfung nur ein
    // Verhindern und kein Unterscheiden.
    vergissAuftrag('rueckfuehren');
    expect(localStorage.getItem(SCHLUESSEL)).toBeNull();
  });

  it('räumt einen unlesbaren Eintrag auch beim Vergessen mit Art weg', () => {
    // Ein verstümmelter Eintrag gehört keiner Art an. Bliebe er liegen,
    // blockierte er den einen Speicherplatz dauerhaft: `merkeAuftrag`
    // überschriebe ihn zwar, aber bis dahin fände `offenerAuftrag` nichts
    // und niemand räumte ihn ab.
    localStorage.setItem(SCHLUESSEL, '{das ist kein JSON');

    vergissAuftrag('anonymisieren');

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
