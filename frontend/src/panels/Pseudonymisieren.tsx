import { useState } from 'react';
import { FileUp, KeyRound, Loader2, ShieldOff, Type } from 'lucide-react';
import BefundListe from '../components/BefundListe';
import DateiFeld from '../components/DateiFeld';
import ErgebnisFeld from '../components/ErgebnisFeld';
import Hinweis from '../components/Hinweis';
import {
  anonymisiereDatei,
  anonymisiereText,
  fehlerMeldung,
  type Befund,
  type Ergebnis,
  type Modus,
} from '../core/api';

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

  const [laeuft, setLaeuft] = useState(false);
  const [wendetAn, setWendetAn] = useState(false);
  const [fehler, setFehler] = useState<string | null>(null);
  const [ergebnis, setErgebnis] = useState<Ergebnis | null>(null);
  /** Der Text, aus dem das aktuelle Ergebnis entstand — Grundlage für „Anwenden“. */
  const [quellText, setQuellText] = useState('');
  const [ausgewaehlt, setAusgewaehlt] = useState<Set<string>>(new Set());

  const kannStarten = quelle === 'text' ? text.trim().length > 0 : datei !== null;

  function uebernimmErgebnis(e: Ergebnis, ursprung: string): void {
    setErgebnis(e);
    setQuellText(ursprung);
    setAusgewaehlt(new Set(e.befunde.map((b: Befund) => b.id)));
  }

  async function starten(): Promise<void> {
    setFehler(null);
    setLaeuft(true);
    try {
      if (quelle === 'datei' && datei) {
        uebernimmErgebnis(await anonymisiereDatei(datei, modus), '');
      } else {
        uebernimmErgebnis(await anonymisiereText(text, modus), text);
      }
    } catch (e) {
      setFehler(fehlerMeldung(e));
      setErgebnis(null);
    } finally {
      setLaeuft(false);
    }
  }

  /** Erneut ersetzen — nur noch mit den Funden, die der Mensch bestätigt hat. */
  async function anwenden(): Promise<void> {
    if (!ergebnis) return;
    if (!quellText) {
      setFehler(
        'Die geprüfte Auswahl lässt sich nur auf Texteingaben anwenden. Bei Dateien starten Sie den Lauf bitte neu.',
      );
      return;
    }
    setFehler(null);
    setWendetAn(true);
    try {
      const auswahl = ergebnis.befunde.filter((b) => ausgewaehlt.has(b.id));
      const neu = await anonymisiereText(quellText, modus, auswahl);
      // Die Auswahl des Menschen bleibt bestehen; nur Text und Vault erneuern sich.
      setErgebnis({ ...neu, befunde: ergebnis.befunde });
    } catch (e) {
      setFehler(fehlerMeldung(e));
    } finally {
      setWendetAn(false);
    }
  }

  function zuruecksetzen(): void {
    setText('');
    setDatei(null);
    setErgebnis(null);
    setQuellText('');
    setAusgewaehlt(new Set());
    setFehler(null);
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
          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Befunde</h2>
            <BefundListe
              befunde={ergebnis.befunde}
              ausgewaehlt={ausgewaehlt}
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
              modus === 'anonym'
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
