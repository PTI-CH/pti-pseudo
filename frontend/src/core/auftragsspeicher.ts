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
 * **Auch nicht für den Dateinamen allein.** Naheliegend wäre, Kennung und Art
 * im `localStorage` zu lassen und nur den Dateinamen — das einzige Feld, das
 * ein Personendatum sein kann — in den `sessionStorage` zu legen. Zwei
 * Eigenschaften des `sessionStorage` sprechen dagegen, und beide zielen genau
 * auf den Zweck dieses Moduls:
 *
 * 1. Er hängt am **Tab**, nicht an der Browsersitzung. Der Name wäre damit in
 *    genau den Fällen fort, für die dieses Modul gebaut wurde (Tab
 *    geschlossen, Browser abgestürzt, Rechner zugeklappt), und nur im
 *    harmlosesten Fall da: dem F5 im selben Tab. Die Wiederaufnahme liefe
 *    dann namenlos — „irgendein Auftrag läuft noch". Das ist der Zustand, der
 *    wie ein Überbleibsel wirkt und den derselbe Mensch mit einem zweiten
 *    Upload derselben grossen Datei beantwortet.
 * 2. Er ist **nicht verlässlich flüchtig**. Die grossen Browser schreiben ihn
 *    auf die Platte und stellen ihn bei „Sitzung wiederherstellen" und nach
 *    einem Absturz wieder her. Die Zusage wäre „meist weg", nicht „weg" — und
 *    eine Zusicherung, die man nicht schreiben kann, ist keine.
 *
 * Der Dateiname bleibt deshalb hier, und die Aufbewahrung wird stattdessen
 * dort begrenzt, wo sie hingehört: beim Abräumen (siehe unten).
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
 * **Was abgeräumt wird — und was nicht.** `HOECHSTALTER_MS` wirkte bis zum
 * 2026-08-26 ausschliesslich **beim Lesen**: Wer die Seite schloss und den
 * Bereich nie wieder öffnete, dessen Dateiname lag unbegrenzt im
 * Browserprofil. {@link raeumeAbgelaufenenAuftragAb} hängt das Abräumen
 * deshalb an den Start der Anwendung (`App.tsx`) statt an einen Bereich.
 * Abgedeckt ist damit:
 *
 * - Werkzeug erneut geöffnet, gleich in welchem Bereich → abgeräumt.
 * - Bereich nie wieder geöffnet, Werkzeug schon → abgeräumt.
 *
 * **Nicht abgedeckt bleibt: das Werkzeug wird nie wieder geöffnet.** Dann
 * läuft in diesem Browserprofil kein Code von uns mehr, und der Eintrag samt
 * Dateiname liegt weiter dort. Kein Mittel im Browser erreicht diesen Fall —
 * weder eine kürzere Frist noch ein zweiter Speicher (siehe oben). Wer ihn
 * schliessen muss, weil das Gerät geteilt wird, hat genau eine Möglichkeit:
 * den Dateinamen gar nicht erst merken und die Wiederaufnahme namenlos
 * anzeigen. Das ist eine Abwägung zulasten der Oberfläche und deshalb heute
 * nicht getroffen — aber sie ist die einzige, die diesen Rest schliesst.
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
 * **Mit `id` vergisst nur, wer seinen eigenen Auftrag beendet.** Es gibt
 * einen einzigen Speicherplatz, aber zwei Bereiche, die gleichzeitig montiert
 * sind (`App.tsx`): Grosser Anonymisierungslauf startet, der Mensch wechselt
 * zu „Rückführen" und startet dort ebenfalls einen grossen Lauf (dessen
 * Eintrag den ersten überschreibt), der Anonymisierungslauf wird fertig und
 * räumt auf. Ein bedingungsloses Vergessen nähme dabei den Eintrag der
 * Rückführung mit, und die wäre nach einem Neuladen unauffindbar — genau der
 * Verlust, gegen den dieses Modul gebaut wurde.
 *
 * **Warum die Kennung und nicht die Art.** Nach Art zu prüfen deckte den Fall
 * oben ab, aber nicht zwei Läufe **derselben** Art: Der zweite überschreibt
 * den Eintrag des ersten, der erste wird fertig, und seine Art passt — er
 * räumte den fremden Eintrag weg. Dass das heute nicht vorkommt, hängt allein
 * an der während eines Laufs gesperrten Oberfläche; diese Absicherung liegt
 * in einem anderen Modul und kann bei der nächsten Änderung an den
 * Bedienelementen wegfallen, ohne dass jemand an das Gedächtnis denkt. Die
 * Kennung ist die Eigenschaft, die einen Auftrag tatsächlich unterscheidet.
 *
 * Ohne `id` wird bedingungslos geräumt. Das ist der Weg für einen Eintrag,
 * der niemandem mehr gehört: abgelaufen oder unlesbar (siehe
 * {@link offenerAuftrag}). Ein Eintrag **ohne** Kennung gehört ebenfalls
 * niemandem und geht auch bei gesetztem `id` weg — sonst bliebe er für immer
 * liegen und blockierte den einen Platz.
 */
export function vergissAuftrag(id?: string): void {
  try {
    const s = speicher();
    if (!s) return;
    if (id) {
      const roh = s.getItem(SCHLUESSEL);
      let eintrag: GemerkterAuftrag | null = null;
      try {
        eintrag = roh ? (JSON.parse(roh) as GemerkterAuftrag) : null;
      } catch {
        eintrag = null;
      }
      if (eintrag?.id && eintrag.id !== id) return;
    }
    s.removeItem(SCHLUESSEL);
  } catch {
    /* siehe oben */
  }
}

/**
 * Liest den Eintrag und räumt ihn weg, wenn er unbrauchbar ist.
 *
 * Die gemeinsame Fassung hinter {@link offenerAuftrag} und
 * {@link raeumeAbgelaufenenAuftragAb}: **Prüfen und Abräumen sind dieselbe
 * Arbeit** — wer prüft, weiss bereits, ob der Eintrag noch jemandem gehört.
 * Zwei Fassungen liefen mit der Zeit auseinander, und die Alterungsgrenze
 * wäre wieder an genau einem Aufrufer festgemacht.
 */
function liesEintrag(): GemerkterAuftrag | null {
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
  return eintrag;
}

/**
 * Der gemerkte Auftrag, sofern es einen gibt und er nicht zu alt ist.
 *
 * Räumt einen abgelaufenen oder unlesbaren Eintrag gleich mit weg: Ein
 * kaputter Eintrag, den niemand entfernt, machte die Wiederaufnahme dauerhaft
 * unbrauchbar.
 */
export function offenerAuftrag(art?: Auftragsart): GemerkterAuftrag | null {
  const eintrag = liesEintrag();
  if (!eintrag) return null;
  // Nach Art gefragt und eine andere gefunden: nicht wegwerfen — das andere
  // Panel wartet womöglich darauf.
  if (art && eintrag.art !== art) return null;
  return eintrag;
}

/**
 * Räumt einen abgelaufenen oder unlesbaren Eintrag ab, ohne ihn zu benutzen.
 *
 * **Für den Menschen, der den Bereich nie wieder öffnet.** Die Alterungsgrenze
 * hing bis zum 2026-08-26 ausschliesslich an {@link offenerAuftrag}, und der
 * wird nur aus den beiden Auftragsbereichen gerufen. Wer das Werkzeug wieder
 * aufmacht, aber in der Begriffsliste bleibt, räumte damit nichts ab — und im
 * Eintrag steht ein Dateiname (siehe Modulkopf). `App.tsx` ruft das hier
 * deshalb beim Start, unabhängig vom gewählten Bereich.
 *
 * Dass die beiden Bereiche heute ohnehin dauerhaft montiert sind und beim
 * Mount lesen, ist **kein Ersatz**: Das Abräumen hinge dann als Nebenwirkung
 * an einer Lesefunktion, die es für die Wiederaufnahme gibt. Würde ein Bereich
 * eines Tages erst bei Bedarf montiert — der Bereich „API-Schlüssel" ist es
 * bereits —, verschwände die Aufbewahrungsgrenze still mit ihm.
 *
 * Ein noch gültiger Eintrag bleibt unangetastet: Er kann zu einem Auftrag
 * gehören, der gerade läuft und den ein Bereich gleich wiederaufnimmt.
 */
export function raeumeAbgelaufenenAuftragAb(): void {
  liesEintrag();
}
