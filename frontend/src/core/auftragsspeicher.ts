/**
 * Merkt sich einen laufenden Auftrag über ein Seitenneuladen hinweg.
 *
 * **Warum es das braucht.** Eine grosse Datei wird zu einem Auftrag: Der
 * Server antwortet mit `202` und einer `job_id`, die Oberfläche fragt im
 * Zwei-Sekunden-Takt nach. Diese Kennung lebte bisher ausschliesslich in
 * einer lokalen Variablen der laufenden Anfrage. Wer die Seite währenddessen
 * neu lud — oder versehentlich F5 drückte, oder dessen Browser den Tab
 * entlud —, verlor sie ersatzlos.
 *
 * Der Auftrag lief dann weiter, war 24 Stunden lang abholbar, und niemand
 * konnte ihn mehr finden: Es gibt keine Liste eigener Aufträge, nur
 * `GET /jobs/{id}`. Die Arbeit war getan und unerreichbar, und der Mensch
 * begann dieselbe grosse Datei von vorn — ein zweiter Auftrag, ein zweiter
 * Tresor, ein zweiter Satz Pseudonyme für dieselben Namen.
 *
 * **Warum `localStorage` und nicht `sessionStorage`.** `sessionStorage` stirbt
 * mit dem Tab. Genau der Fall, um den es geht (Tab geschlossen, Browser
 * abgestürzt, Rechner zugeklappt), wäre damit nicht abgedeckt.
 *
 * **Was darin liegt — und warum das eine Abwägung ist, keine Unbedenklichkeit.**
 * Gespeichert werden die Auftragskennung, die Art und der Dateiname.
 * Dokumentinhalte stehen hier nie.
 *
 * Der Dateiname ist die Abwägung. Er *kann* ein Personendatum sein — an
 * anderer Stelle im Baum ist genau das der ausdrückliche Grund, ihn nicht
 * zu speichern: `usage_events` bekommt ihn nicht, weil
 * „Einvernahme_Muster_Hans.docx" ein Name in einer unverschlüsselten,
 * auswertbaren Tabelle wäre (siehe
 * `backend/tests/test_jobs_ausfuehrung.py::test_der_nutzungseintrag_traegt_keinen_dateinamen`).
 * Hier ist die Lage eine andere, und deshalb fällt die Abwägung anders aus:
 * Der Name stammt aus der Auswahl desselben Menschen an seinem eigenen
 * Gerät, er verlässt es nicht, und ohne ihn kann die Oberfläche nach einem
 * Neuladen nicht sagen, WELCHE Datei noch läuft — der Fortschrittshinweis
 * wirkte wie ein Überbleibsel, und derselbe Mensch lüde dieselbe grosse
 * Datei ein zweites Mal hoch.
 *
 * **Die offene Flanke ist die Aufbewahrung.** `HOECHSTALTER_MS` wirkt nur
 * **beim Lesen**: Wer die Seite schliesst und den Bereich nie wieder
 * öffnet, dessen Dateiname liegt unbegrenzt im Browserprofil — niemand
 * räumt ihn ab. Auf einem persönlichen Gerät ist das der Normalfall und
 * belanglos; auf einem geteilten Arbeitsplatz mit einem gemeinsamen
 * Browserprofil ist es die Frage, die man stellen muss. Wer sie mit „nein"
 * beantwortet, braucht ein Abräumen unabhängig vom Lesen (Aufräumen beim
 * Seitenstart, oder gar kein Dateiname im Speicher) — nicht bloss eine
 * kürzere Frist.
 */

/** Welche der beiden Dateirouten den Auftrag angelegt hat. */
export type Auftragsart = 'anonymisieren' | 'rueckfuehren';

export interface GemerkterAuftrag {
  id: string;
  art: Auftragsart;
  dateiname: string;
  /** Zeitpunkt der Anlage in Millisekunden — für die Alterungsgrenze unten. */
  angelegt: number;
}

const SCHLUESSEL = 'pti-pseudo.offener-auftrag';

/**
 * Ab wann ein gemerkter Eintrag verworfen wird.
 *
 * Muss zur Aufbewahrung des Servers passen (`warteschlange.AUFBEWAHRUNG`,
 * 24 Stunden). Ein älterer Eintrag zeigt auf einen Auftrag, den der Server
 * längst entfernt hat — ohne diese Grenze begrüsste die Oberfläche den
 * Nutzer bei jedem Start mit einer Nachfrage, die zuverlässig in einer 404
 * endet.
 */
const HOECHSTALTER_MS = 24 * 60 * 60 * 1000;

/**
 * Jeder Zugriff in `try`/`catch`.
 *
 * `localStorage` ist nicht überall vorhanden und wirft in manchen
 * Zuständen schon beim blossen Lesen — privates Fenster, blockierte
 * Website-Daten, voller Speicher. Ein Auftragsgedächtnis ist eine
 * Bequemlichkeit; es darf die Oberfläche unter keinen Umständen
 * mitreissen.
 */
function speicher(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Merkt einen soeben angelegten Auftrag. */
export function merkeAuftrag(auftrag: Omit<GemerkterAuftrag, 'angelegt'>): void {
  try {
    speicher()?.setItem(
      SCHLUESSEL,
      JSON.stringify({ ...auftrag, angelegt: Date.now() } satisfies GemerkterAuftrag),
    );
  } catch {
    /* Kein Gedächtnis ist besser als ein Absturz. */
  }
}

/**
 * Vergisst den gemerkten Auftrag — nach Abschluss, Fehler oder Abbruch.
 *
 * **Mit `art` wird nur der Auftrag DIESER Art vergessen.** Es gibt einen
 * einzigen Speicherplatz, aber zwei Bereiche, die gleichzeitig montiert sind
 * (`App.tsx`). Ohne die Prüfung räumte das Ende des einen Laufs den Eintrag
 * des anderen weg: Grosser Anonymisierungslauf startet, der Mensch wechselt
 * zu „Rückführen" und startet dort ebenfalls einen grossen Lauf (dessen
 * Eintrag den ersten überschreibt), der Anonymisierungslauf wird fertig und
 * löscht beim Aufräumen den Eintrag der Rückführung. Nach einem Neuladen
 * wäre die noch laufende Rückführung unauffindbar — genau der Verlust,
 * gegen den dieses Modul gebaut wurde.
 *
 * Dasselbe gilt für „Zurücksetzen" im einen Bereich, während im anderen ein
 * Auftrag läuft.
 *
 * Ohne `art` wird bedingungslos geräumt. Das ist der Weg für einen Eintrag,
 * der niemandem mehr gehört: abgelaufen oder unlesbar (siehe
 * {@link offenerAuftrag}).
 */
export function vergissAuftrag(art?: Auftragsart): void {
  try {
    const s = speicher();
    if (!s) return;
    if (art) {
      const roh = s.getItem(SCHLUESSEL);
      let eintrag: GemerkterAuftrag | null = null;
      try {
        eintrag = roh ? (JSON.parse(roh) as GemerkterAuftrag) : null;
      } catch {
        // Ein unlesbarer Eintrag gehört niemandem — der darf weg, sonst
        // bliebe er für immer liegen und blockierte den einen Platz.
        eintrag = null;
      }
      if (eintrag && eintrag.art !== art) return;
    }
    s.removeItem(SCHLUESSEL);
  } catch {
    /* siehe oben */
  }
}

/**
 * Der gemerkte Auftrag, sofern es einen gibt und er nicht zu alt ist.
 *
 * Räumt einen abgelaufenen oder unlesbaren Eintrag gleich mit weg: Ein
 * kaputter Eintrag, den niemand entfernt, machte die Wiederaufnahme dauerhaft
 * unbrauchbar.
 */
export function offenerAuftrag(art?: Auftragsart): GemerkterAuftrag | null {
  const roh = (() => {
    try {
      return speicher()?.getItem(SCHLUESSEL) ?? null;
    } catch {
      return null;
    }
  })();
  if (!roh) return null;

  let eintrag: GemerkterAuftrag;
  try {
    eintrag = JSON.parse(roh) as GemerkterAuftrag;
  } catch {
    vergissAuftrag();
    return null;
  }
  if (!eintrag?.id || typeof eintrag.angelegt !== 'number') {
    vergissAuftrag();
    return null;
  }
  if (Date.now() - eintrag.angelegt > HOECHSTALTER_MS) {
    vergissAuftrag();
    return null;
  }
  // Nach Art gefragt und eine andere gefunden: nicht wegwerfen — das andere
  // Panel wartet womöglich darauf.
  if (art && eintrag.art !== art) return null;
  return eintrag;
}
