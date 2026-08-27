import { useEffect, useState } from 'react';
import { FileUp, KeyRound, Loader2, ShieldOff, Type } from 'lucide-react';
import BefundListe from '../components/BefundListe';
import DateiFeld from '../components/DateiFeld';
import ErgebnisFeld from '../components/ErgebnisFeld';
import Hinweis from '../components/Hinweis';
import KategorienWahl from '../components/KategorienWahl';
import {
  anonymisiereDatei,
  anonymisiereText,
  fehlerMeldung,
  gibErgebnisFrei,
  ladeKategorien,
  verfolgeAuftrag,
  type Befund,
  type Ergebnis,
  type Kategorie,
  type Modus,
} from '../core/api';
import { offenerAuftrag, vergissAuftrag } from '../core/auftragsspeicher';

type Quelle = 'text' | 'datei';

/**
 * Bereich 1 + 2: Pseudonymisieren und die Befund-Ansicht mit der Ampel.
 *
 * Beides liegt bewusst auf einer Fläche: die Befunde entstehen aus dem Lauf und
 * werden direkt darunter geprüft, ohne Seitenwechsel.
 */
export default function Pseudonymisieren() {
  const [quelle, setQuelle] = useState<Quelle>('text');
  const [text, setText] = useState('');
  const [datei, setDatei] = useState<File | null>(null);
  const [modus, setModus] = useState<Modus>('umkehrbar');

  const [kategorien, setKategorien] = useState<Kategorie[]>([]);
  const [kategorienLaden, setKategorienLaden] = useState(true);
  const [kategorienFehler, setKategorienFehler] = useState<string | null>(null);
  const [gewaehlteKategorien, setGewaehlteKategorien] = useState<Set<string>>(new Set());

  const [laeuft, setLaeuft] = useState(false);
  const [wendetAn, setWendetAn] = useState(false);
  const [fehler, setFehler] = useState<string | null>(null);
  const [ergebnis, setErgebnis] = useState<Ergebnis | null>(null);
  /**
   * Stand eines im Hintergrund laufenden Auftrags (grosse Datei, Server
   * antwortet mit 202). `null` ausserhalb dieses Wartezustands.
   *
   * Bewusst getrennt von `laeuft`/`wendetAn`: die beiden bleiben während des
   * gesamten Auftrags gesetzt (deaktivieren die Bedienelemente), `auftrag`
   * steuert nur, ob der Fortschritts-Hinweis erscheint.
   */
  const [auftrag, setAuftrag] = useState<{
    fortschritt: number | null;
    gesamt: number | null;
  } | null>(null);
  /**
   * Der Dateiname eines Auftrags, der ein Seitenneuladen überlebt hat.
   *
   * Nur für den Hinweistext: Nach einem Neuladen ist die Datei selbst weg
   * (ein `File` überlebt kein Neuladen), der Auftrag auf dem Server aber
   * nicht. Der Mensch soll lesen, WELCHE Datei da noch läuft — sonst wirkt
   * der Fortschrittshinweis wie ein Überbleibsel und er lädt dieselbe Datei
   * ein zweites Mal hoch.
   */
  const [wiederaufgenommen, setWiederaufgenommen] = useState<string | null>(null);
  /** Der Text, aus dem das aktuelle Ergebnis entstand — Grundlage für „Anwenden“. */
  const [quellText, setQuellText] = useState('');
  /**
   * Die Kategorien, mit denen der aktuelle Befund entstanden ist.
   *
   * Bewusst eine eigene Kopie und nicht `gewaehlteKategorien`: die Checkboxen
   * bleiben nach dem Lauf bedienbar, und wer dort etwas ändert und danach
   * „Anwenden“ drückt, schickte sonst eine Kategorienliste, die nicht mehr zu
   * den angezeigten Funden passt — der Server sucht dann anders, als die Liste
   * darüber behauptet.
   */
  const [laufKategorien, setLaufKategorien] = useState<string[]>([]);
  /**
   * Die Quelle, aus der der aktuelle Befund entstand.
   *
   * Aus demselben Grund eine eigene Kopie wie `laufKategorien`: der Umschalter
   * Text ⇄ Datei bleibt nach dem Lauf bedienbar. Wer dort wechselt und dann
   * „Anwenden“ drückt, schickte sonst eine Auswahl an die falsche Quelle.
   */
  const [laufQuelle, setLaufQuelle] = useState<Quelle>('text');
  /**
   * Der Modus, mit dem der aktuelle Befund entstand.
   *
   * Dieselbe Begründung wie bei `laufKategorien` und `laufQuelle`, und hier
   * wiegt sie am schwersten: Wer nach dem Lauf von „umkehrbar“ auf „anonym“
   * umschaltet und dann „Anwenden“ drückt, bekäme eine Ersetzung ohne Tresor —
   * die geprüfte Fundliste sähe unverändert aus, aber die Rückführung wäre
   * unmöglich geworden, ohne dass es jemand angeordnet hätte.
   *
   * `null` heisst **unbekannt**: Ein wiederaufgenommener Auftrag aus der Zeit
   * vor `mode` im Auftragsumschlag sagt nicht, womit er lief. Dann wird
   * „Anwenden" verweigert statt geraten — die Vorgabe `umkehrbar` legte für
   * einen `anonym`-Lauf einen Tresor an, also Klarnamen in einer
   * Zuordnungstabelle, die niemand bestellt hat.
   */
  const [laufModus, setLaufModus] = useState<Modus | null>(null);
  /**
   * Ob die KI-Prüfung der Namensgrenzen zugeschaltet ist.
   *
   * Vorangehakt: Die bessere Erkennung soll der Normalfall sein. Wer ein
   * besonders heikles Dokument hat, nimmt das Häkchen weg und behält trotzdem
   * die volle lokale Erkennung.
   */
  const [kiPruefung, setKiPruefung] = useState(true);
  /** Der Stand der KI-Prüfung beim Lauf — wie `laufModus`. */
  const [laufKiPruefung, setLaufKiPruefung] = useState(true);
  const [ausgewaehlt, setAusgewaehlt] = useState<Set<string>>(new Set());

  // Katalog beim Mount laden. Ohne Katalog gibt es keine sinnvolle Auswahl —
  // dann bleibt die Liste leer und der Start-Knopf deaktiviert (siehe
  // kannStarten unten), statt mit einer stillen Vollauswahl zu raten.
  useEffect(() => {
    let abgebrochen = false;
    (async () => {
      setKategorienLaden(true);
      setKategorienFehler(null);
      try {
        const geladen = await ladeKategorien();
        if (abgebrochen) return;
        setKategorien(geladen);
        setGewaehlteKategorien(
          new Set(geladen.filter((k) => k.default_policy === 'redact').map((k) => k.entity_type)),
        );
      } catch (e) {
        if (abgebrochen) return;
        setKategorienFehler(fehlerMeldung(e));
        setKategorien([]);
      } finally {
        if (!abgebrochen) setKategorienLaden(false);
      }
    })();
    return () => {
      abgebrochen = true;
    };
  }, []);

  /**
   * Einen Auftrag wiederaufnehmen, der ein Seitenneuladen überlebt hat.
   *
   * Eine grosse Datei wird zu einem Auftrag; die Kennung lebte bisher nur in
   * der laufenden Anfrage. Wer die Seite dazwischen neu lud, verlor sie —
   * der Auftrag lief weiter, war 24 Stunden abholbar, und niemand konnte ihn
   * mehr finden: Es gibt keine Liste eigener Aufträge, nur `GET /jobs/{id}`.
   * Die Arbeit war getan und unerreichbar, und der Mensch begann dieselbe
   * grosse Datei von vorn.
   *
   * Ohne Abfrage, ohne Knopf: Der Mensch hat diesen Lauf gestartet und
   * wartet darauf. Ihn zu fragen, ob er sein eigenes Ergebnis haben möchte,
   * wäre eine Höflichkeit, die ihm nur eine Entscheidung mehr abverlangt.
   *
   * Der Lauf setzt `laeuft`, damit die Bedienelemente währenddessen gesperrt
   * sind — genau wie bei einem selbst gestarteten Lauf, und aus demselben
   * Grund: Ein zweiter Start daneben legte einen zweiten Auftrag an.
   */
  useEffect(() => {
    const offen = offenerAuftrag('anonymisieren');
    if (!offen) return;

    let abgebrochen = false;
    setLaeuft(true);
    setWiederaufgenommen(offen.dateiname);
    (async () => {
      try {
        const e = await verfolgeAuftrag(
          offen.id,
          'anonymisieren',
          offen.dateiname,
          (fortschritt, gesamt) => {
            if (!abgebrochen) setAuftrag({ fortschritt, gesamt });
          },
        );
        if (abgebrochen) return;
        // `quellText` bleibt leer und `laufQuelle` steht auf 'datei': Nach
        // einem Neuladen ist die Datei selbst weg. Der Befund lässt sich
        // ansehen und das Ergebnis herunterladen; „Anwenden" verlangt die
        // Datei erneut und sagt das auch (siehe `anwenden`).
        //
        // Kategorien, Modus und KI-Häkchen kommen aus dem ERGEBNIS, nicht aus
        // den Zustandsvariablen: Dieser Effekt hat `[]`-Deps und liest damit
        // die Closure des Mounts. Dort ist `gewaehlteKategorien` noch leer
        // (der Katalog lädt erst danach) und `modus` steht auf dem
        // Vorgabewert `umkehrbar`. Mit diesen Werten wäre „Anwenden" bei
        // einem wiederaufgenommenen Lauf zweimal falsch: Es bräche mit
        // „Wählen Sie mindestens eine Kategorie aus" ab, obwohl in der
        // Oberfläche eine angehakt ist — und wer einen `anonym`-Lauf
        // wiederaufnähme, legte damit einen Tresor an, den er nicht bestellt
        // hat. Der Auftragsumschlag trägt diese drei Angaben genau deswegen
        // (`backend/jobs/ausfuehrung.py`).
        uebernimmErgebnis(
          e,
          '',
          e.laufKategorien ?? [],
          'datei',
          e.laufModus,
          // Unbekannt → `false`: Die Richtung ist nicht beliebig. `true`
          // schickte beim nächsten Aufruf Textausschnitte an den KI-Dienst,
          // die der Mensch bei seinem Lauf womöglich abgewählt hatte; `false`
          // kostet höchstens Erkennungsgüte in einem Lauf, den er selbst
          // auslöst. Und die Warnung „nur lokal geprüft" bliebe sonst für
          // einen Lauf stehen, von dem niemand weiss, ob er sie verdient.
          e.laufKiPruefung ?? false,
        );
      } catch (e) {
        if (abgebrochen) return;
        // Auch ein 404 landet hier: Der Auftrag ist abgelaufen oder wurde
        // aufgeräumt. `verfolgeAuftrag` hat den Eintrag dabei schon
        // vergessen; der Hinweis erklärt dem Menschen, warum plötzlich
        // etwas von einem Auftrag stand.
        setFehler(fehlerMeldung(e));
      } finally {
        if (!abgebrochen) {
          setLaeuft(false);
          setAuftrag(null);
          setWiederaufgenommen(null);
        }
      }
    })();
    return () => {
      // Verlässt der Mensch den Bereich, hört nur die ANZEIGE auf. Der
      // Auftrag läuft weiter und bleibt gemerkt — deshalb hier kein
      // `vergissAuftrag()`.
      abgebrochen = true;
    };
    // Absichtlich nur beim Mount: Der Auftrag wird einmal aufgenommen, nicht
    // bei jeder Änderung an Kategorien oder Modus erneut. Dass die Closure
    // damit veraltet ist, schadet seit oben nicht mehr — der Effekt liest
    // keine Bedienelemente, alles Nötige steht im Auftragsergebnis.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const kannStarten =
    (quelle === 'text' ? text.trim().length > 0 : datei !== null) && gewaehlteKategorien.size > 0;

  function uebernimmErgebnis(
    e: Ergebnis,
    ursprung: string,
    kategorien: string[],
    quelleDesLaufs: Quelle,
    /** `null` nur bei einem wiederaufgenommenen Auftrag ohne `mode` im Umschlag. */
    modusDesLaufs: Modus | null,
    kiDesLaufs: boolean,
  ): void {
    // Eine alte Blob-URL (Dateiergebnis) lebt nur im Browser-Speicher — wird
    // sie durch ein neues Ergebnis ersetzt, ohne dass sie je heruntergeladen
    // wurde, bliebe sie sonst bis zum Reload im Speicher hängen.
    gibErgebnisFrei(ergebnis);
    setErgebnis(e);
    setQuellText(ursprung);
    setLaufKategorien(kategorien);
    setLaufQuelle(quelleDesLaufs);
    setLaufModus(modusDesLaufs);
    setLaufKiPruefung(kiDesLaufs);
    setAusgewaehlt(new Set(e.befunde.map((b: Befund) => b.id)));
  }

  async function starten(): Promise<void> {
    setFehler(null);
    setLaeuft(true);
    setAuftrag(null);
    try {
      const kategorienListe = Array.from(gewaehlteKategorien);
      if (quelle === 'datei' && datei) {
        const e = await anonymisiereDatei(
          datei,
          modus,
          kategorienListe,
          kiPruefung,
          undefined,
          undefined,
          (fortschritt, gesamt) => setAuftrag({ fortschritt, gesamt }),
        );
        uebernimmErgebnis(e, '', kategorienListe, 'datei', modus, kiPruefung);
      } else {
        const e = await anonymisiereText(text, modus, kategorienListe, kiPruefung);
        uebernimmErgebnis(e, text, kategorienListe, 'text', modus, kiPruefung);
      }
    } catch (e) {
      // Ein fehlgeschlagener Lauf verwirft ein evtl. noch vorhandenes altes
      // Dateiergebnis — dessen Blob-URL muss vor dem Verwerfen freigegeben
      // werden, sonst bleibt sie bis zum Reload im Speicher hängen.
      gibErgebnisFrei(ergebnis);
      setFehler(fehlerMeldung(e));
      setErgebnis(null);
    } finally {
      setLaeuft(false);
      setAuftrag(null);
    }
  }

  /**
   * Erneut ersetzen — nur noch mit den Funden, die der Mensch bestätigt hat.
   *
   * Gilt für beide Quellen. Bei einer Datei wird sie dazu erneut hochgeladen:
   * der Server hält bewusst keine Dokument-Bytes vor, es gibt also nichts,
   * worauf sich ein zweiter Aufruf beziehen könnte.
   */
  async function anwenden(): Promise<void> {
    if (!ergebnis) return;
    // Steht vor allen anderen Prüfungen: Ist der Modus des Laufs unbekannt,
    // hilft es nicht, die Datei erneut zu wählen. Der Fall trifft einen
    // wiederaufgenommenen Auftrag, der noch aus der Zeit vor `mode` im
    // Auftragsumschlag stammt (nach einem Deploy bis zu 24 Stunden lang
    // möglich). Geraten wird hier nichts: Die Vorgabe `umkehrbar` legte für
    // einen `anonym`-Lauf einen Tresor an — Klarnamen in einer
    // Zuordnungstabelle, die niemand bestellt hat.
    if (laufModus === null) {
      setFehler(
        'Zu diesem Auftrag ist nicht überliefert, mit welchem Modus und welchen ' +
          'Kategorien er gelaufen ist — er stammt aus einer älteren Fassung des ' +
          'Werkzeugs. Das Ergebnis oben können Sie unverändert herunterladen. Um ' +
          'Fehlalarme abzuwählen, starten Sie den Lauf bitte neu.',
      );
      return;
    }
    if (laufQuelle === 'datei' && !datei) {
      setFehler('Die Datei ist nicht mehr geladen. Bitte wählen Sie sie erneut aus.');
      return;
    }
    if (laufQuelle === 'text' && !quellText) {
      setFehler('Der Ausgangstext ist nicht mehr vorhanden. Bitte starten Sie den Lauf neu.');
      return;
    }
    if (laufKategorien.length === 0) {
      setFehler('Wählen Sie mindestens eine Kategorie aus, bevor Sie starten.');
      return;
    }
    setFehler(null);
    setWendetAn(true);
    setAuftrag(null);
    try {
      const auswahl = ergebnis.befunde.filter((b) => ausgewaehlt.has(b.id));
      // Bewusst `laufKategorien` und `laufModus` statt der aktuellen
      // Bedienelemente: der Befund stammt aus dem Lauf, die Ersetzung muss zu
      // ihm passen. Beide bleiben nach dem Lauf bedienbar.
      const neu =
        laufQuelle === 'datei' && datei
          ? await anonymisiereDatei(
              datei,
              laufModus,
              laufKategorien,
              laufKiPruefung,
              auswahl,
              undefined,
              (fortschritt, gesamt) => setAuftrag({ fortschritt, gesamt }),
            )
          : await anonymisiereText(
              quellText, laufModus, laufKategorien, laufKiPruefung, auswahl);
      // Die alte Blob-URL wird durch die neue ersetzt und muss vorher weg.
      gibErgebnisFrei(ergebnis);
      // Die Auswahl des Menschen bleibt bestehen; nur Ergebnis und Vault erneuern sich.
      setErgebnis({ ...neu, befunde: ergebnis.befunde });
    } catch (e) {
      setFehler(fehlerMeldung(e));
    } finally {
      setWendetAn(false);
      setAuftrag(null);
    }
  }

  function zuruecksetzen(): void {
    // Auch das Auftragsgedächtnis: „Zurücksetzen" heisst, dass der Mensch
    // mit diesem Lauf fertig ist. Bliebe der Eintrag stehen, begrüsste ihn
    // der nächste Seitenaufruf mit einem Auftrag, den er gerade verworfen hat.
    //
    // Vergessen wird nach Kennung (`vergissAuftrag`), und dieser Knopf kennt
    // keine: Ein Auftrag entsteht tief in `api.anonymisiereDatei`, seine
    // `job_id` kommt hier nie an. Geholt wird sie deshalb aus dem eigenen
    // Eintrag — `offenerAuftrag('anonymisieren')` liefert nur einen Eintrag
    // dieses Bereichs und lässt den des Bereichs „Rückführen" unberührt, der
    // auf demselben einen Speicherplatz stehen kann. Steht dort nichts oder
    // Fremdes, gibt es hier nichts zu vergessen.
    const eigener = offenerAuftrag('anonymisieren');
    if (eigener) vergissAuftrag(eigener.id);
    setWiederaufgenommen(null);
    gibErgebnisFrei(ergebnis);
    setText('');
    setDatei(null);
    setErgebnis(null);
    setQuellText('');
    setLaufKategorien([]);
    setLaufQuelle('text');
    // `null` wie beim Start: Es gibt keinen Lauf mehr, dessen Modus hier
    // stünde — und kein Ergebnis, das ihn benutzte.
    setLaufModus(null);
    setLaufKiPruefung(true);
    setAusgewaehlt(new Set());
    setFehler(null);
    setAuftrag(null);
  }

  return (
    <div className="space-y-6">
      {/* ── Eingabe ───────────────────────────────────────────────────────── */}
      <section className="space-y-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Pseudonymisieren</h2>

        {/* Umschalter Text ⇄ Datei */}
        <div className="inline-flex rounded-lg border border-gray-300 dark:border-gray-600 overflow-hidden">
          {(
            [
              ['text', 'Text', Type],
              ['datei', 'Datei', FileUp],
            ] as const
          ).map(([wert, beschriftung, Symbol]) => (
            <button
              key={wert}
              type="button"
              onClick={() => setQuelle(wert)}
              className={`inline-flex items-center gap-1.5 px-4 py-2 text-sm transition-colors ${
                quelle === wert
                  ? 'bg-blue-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
              }`}
            >
              <Symbol className="w-4 h-4" />
              {beschriftung}
            </button>
          ))}
        </div>

        {quelle === 'text' ? (
          <div>
            <label
              htmlFor="eingabetext"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Zu pseudonymisierender Text
            </label>
            <textarea
              id="eingabetext"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={10}
              placeholder="Fügen Sie hier den Text ein, der Personendaten enthält."
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {text.length.toLocaleString('de-CH')} Zeichen
            </p>
          </div>
        ) : (
          <DateiFeld
            datei={datei}
            onDatei={setDatei}
            onFehler={setFehler}
            deaktiviert={laeuft}
          />
        )}
      </section>

      {/* ── Kategorien ────────────────────────────────────────────────────── */}
      <section className="space-y-3">
        {kategorienLaden ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">Kategorien werden geladen …</p>
        ) : kategorienFehler ? (
          <Hinweis ton="fehler">
            Die Kategorienliste konnte nicht geladen werden: {kategorienFehler} Ohne Kategorien ist
            kein Lauf möglich — laden Sie die Seite neu.
          </Hinweis>
        ) : (
          <KategorienWahl
            kategorien={kategorien}
            gewaehlt={gewaehlteKategorien}
            onGewaehlt={setGewaehlteKategorien}
            deaktiviert={laeuft}
          />
        )}

        {/* KI-Pruefung. Steht bei den Kategorien, weil sie zur Wahl VOR dem
            Lauf gehoert — nicht zum Ergebnis. */}
        <label className="mt-4 flex items-start gap-2 text-sm cursor-pointer">
          <input
            type="checkbox"
            checked={kiPruefung}
            onChange={(e) => setKiPruefung(e.target.checked)}
            disabled={laeuft}
            className="mt-0.5 rounded border-gray-300 dark:border-gray-600"
          />
          <span className="text-gray-700 dark:text-gray-300">
            KI-Prüfung der Namensgrenzen
            <span className="block text-xs text-gray-500 dark:text-gray-400">
              Findet unvollständig erkannte Namen (z.&nbsp;B. „Sandra“ statt
              „Sandra Zbinden“). Dabei verlassen <strong>kurze Textausschnitte</strong>
              {' '}um die gefundenen Stellen den Server und gehen an einen Schweizer
              KI-Dienst. Ohne Häkchen läuft die Erkennung vollständig lokal.
            </span>
          </span>
        </label>
      </section>

      {/* ── Modus ─────────────────────────────────────────────────────────── */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">Modus</h3>
        <div className="grid gap-3 sm:grid-cols-2">
          <ModusKarte
            gewaehlt={modus === 'umkehrbar'}
            onWaehlen={() => setModus('umkehrbar')}
            Symbol={KeyRound}
            titel="Umkehrbar"
            beschreibung="Die Zuordnung Original → Pseudonym wird verschlüsselt gespeichert (Vault). Personen erscheinen als [[PERSON_1]] und lassen sich im Bereich „Rückführen“ wieder in Klartext verwandeln."
          />
          <ModusKarte
            gewaehlt={modus === 'anonym'}
            onWaehlen={() => setModus('anonym')}
            Symbol={ShieldOff}
            titel="Anonym"
            beschreibung="Es wird nichts gespeichert. Die Personendaten sind danach endgültig weg — eine Rückführung ist ausgeschlossen, auch für Sie selbst."
          />
        </div>
        <Hinweis ton="info">
          <p className="font-medium">Welchen Modus brauche ich?</p>
          <p className="mt-1">
            Wählen Sie <strong>umkehrbar</strong>, wenn Sie das Ergebnis später wieder den echten
            Personen zuordnen müssen — etwa, weil Sie den Text extern bearbeiten lassen und danach
            intern weiterverwenden. Wählen Sie <strong>anonym</strong>, wenn das Dokument die
            Organisation dauerhaft verlässt (Veröffentlichung, Statistik, Weitergabe). Anonym ist
            der sichere Weg: Was nicht gespeichert ist, kann auch nicht wiederhergestellt werden.
          </p>
        </Hinweis>
      </section>

      {!kategorienLaden && !kategorienFehler && gewaehlteKategorien.size === 0 && (
        <Hinweis ton="warnung">
          Wählen Sie mindestens eine Kategorie aus, bevor Sie starten.
        </Hinweis>
      )}

      {auftrag && (
        <Hinweis ton="info">
          {wiederaufgenommen ? (
            <>
              Ein Auftrag von vorhin läuft noch: <strong>{wiederaufgenommen}</strong>
            </>
          ) : (
            'Die Datei wird im Hintergrund verarbeitet'
          )}
          {auftrag.gesamt ? ` — Seite ${auftrag.fortschritt} von ${auftrag.gesamt}` : ''}.
          Sie können das Fenster offen lassen; die Fundliste erscheint, sobald der Lauf fertig
          ist.
        </Hinweis>
      )}

      {fehler && <Hinweis ton="fehler">{fehler}</Hinweis>}

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => void starten()}
          disabled={!kannStarten || laeuft}
          className="inline-flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          {laeuft && <Loader2 className="w-4 h-4 animate-spin" />}
          {laeuft ? 'Wird verarbeitet …' : 'Pseudonymisieren'}
        </button>
        <button
          type="button"
          onClick={zuruecksetzen}
          disabled={laeuft}
          className="px-4 py-2 text-sm border border-gray-300 dark:border-gray-600 dark:text-gray-300 rounded hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
        >
          Zurücksetzen
        </button>
      </div>

      {/* ── Befund-Ansicht + Ergebnis ─────────────────────────────────────── */}
      {ergebnis && (
        <div className="space-y-6 border-t border-gray-200 dark:border-gray-700 pt-6">
          {/* Angefordert, aber nicht gelaufen. Der Hinweis steht bei der
              Fundliste und nicht in der Fusszeile: Wer taeglich damit
              arbeitet, ueberliest eine Fusszeile. */}
          {laufKiPruefung && !ergebnis.kiGeprueft && (
            <Hinweis ton="warnung">
              Die KI-Prüfung war nicht erreichbar — dieses Ergebnis wurde{' '}
              <strong>nur lokal geprüft</strong>. Sehen Sie die Fundliste besonders
              sorgfältig auf unvollständig erkannte Namen durch: Ein halb erkannter
              Name bleibt im Dokument stehen, ohne dass man es dem Ergebnis ansieht.
            </Hinweis>
          )}

          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Befunde</h2>
            <BefundListe
              befunde={ergebnis.befunde}
              ausgewaehlt={ausgewaehlt}
              angewendet={ergebnis.angewendet}
              onAusgewaehlt={setAusgewaehlt}
              onAnwenden={() => void anwenden()}
              laeuft={wendetAn}
            />
          </section>

          {ergebnis.vaultId && (
            <Hinweis ton="warnung">
              Die Zuordnungstabelle wurde unter der Kennung{' '}
              <code className="font-mono">{ergebnis.vaultId}</code> gespeichert. Ohne sie ist keine
              Rückführung möglich — notieren Sie sie zusammen mit dem Dokument.
            </Hinweis>
          )}

          <ErgebnisFeld
            titel="Pseudonymisiertes Ergebnis"
            text={ergebnis.text}
            downloadUrl={ergebnis.downloadUrl}
            dateiname={ergebnis.dateiname}
            fussnote={
              // `laufModus`, nicht `modus`: die Fussnote beschreibt das Ergebnis,
              // das hier steht. Sonst behauptet sie nach einem Umschalten der
              // Radiobuttons „keine Zuordnung gespeichert“, während direkt
              // darüber die Tresor-Kennung des Laufs angezeigt wird.
              laufModus === 'anonym'
                ? 'Modus „anonym“: Es wurde keine Zuordnung gespeichert. Dieses Ergebnis lässt sich nicht zurückführen.'
                : undefined
            }
          />
        </div>
      )}
    </div>
  );
}

interface ModusKarteProps {
  gewaehlt: boolean;
  onWaehlen: () => void;
  Symbol: typeof KeyRound;
  titel: string;
  beschreibung: string;
}

function ModusKarte({ gewaehlt, onWaehlen, Symbol, titel, beschreibung }: ModusKarteProps) {
  return (
    <button
      type="button"
      onClick={onWaehlen}
      aria-pressed={gewaehlt}
      className={`text-left rounded-lg border px-4 py-3 transition-colors ${
        gewaehlt
          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
          : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50'
      }`}
    >
      <span className="flex items-center gap-2">
        <Symbol
          className={`w-4 h-4 shrink-0 ${
            gewaehlt ? 'text-blue-600 dark:text-blue-400' : 'text-gray-400 dark:text-gray-500'
          }`}
        />
        <span
          className={`text-sm font-medium ${
            gewaehlt
              ? 'text-blue-700 dark:text-blue-400'
              : 'text-gray-900 dark:text-gray-100'
          }`}
        >
          {titel}
        </span>
      </span>
      <span className="mt-1.5 block text-xs text-gray-500 dark:text-gray-400">{beschreibung}</span>
    </button>
  );
}
