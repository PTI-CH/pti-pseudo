import { useEffect, useState } from 'react';
import { FileUp, Loader2, RefreshCw, Type } from 'lucide-react';
import DateiFeld from '../components/DateiFeld';
import ErgebnisFeld from '../components/ErgebnisFeld';
import Hinweis from '../components/Hinweis';
import {
  fehlerMeldung,
  gibErgebnisFrei,
  ladeVaults,
  rueckfuehrenDatei,
  rueckfuehrenText,
  type Ergebnis,
  type Vault,
} from '../core/api';

type Quelle = 'text' | 'datei';

function datumLesbar(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('de-CH', { dateStyle: 'short', timeStyle: 'short' });
}

/** Beschriftung eines Vaults in der Auswahlliste. */
function vaultBeschriftung(v: Vault): string {
  const teile = [v.bezeichnung || v.id];
  const datum = datumLesbar(v.erstelltAm);
  if (datum) teile.push(datum);
  if (v.anzahlEintraege !== null) teile.push(`${v.anzahlEintraege} Einträge`);
  return teile.join(' · ');
}

/** Bereich 3: Pseudonyme wieder in Klartext verwandeln. */
export default function Rueckfuehren() {
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [vaultsLaden, setVaultsLaden] = useState(true);
  const [vaultsFehler, setVaultsFehler] = useState<string | null>(null);
  const [vaultId, setVaultId] = useState('');

  const [quelle, setQuelle] = useState<Quelle>('text');
  const [text, setText] = useState('');
  const [datei, setDatei] = useState<File | null>(null);

  const [laeuft, setLaeuft] = useState(false);
  const [fehler, setFehler] = useState<string | null>(null);
  const [ergebnis, setErgebnis] = useState<Ergebnis | null>(null);

  async function vaultsLaden_() {
    setVaultsLaden(true);
    setVaultsFehler(null);
    try {
      setVaults(await ladeVaults());
    } catch (e) {
      setVaultsFehler(fehlerMeldung(e));
      setVaults([]);
    } finally {
      setVaultsLaden(false);
    }
  }

  useEffect(() => {
    void vaultsLaden_();
  }, []);

  const kannStarten =
    vaultId.trim().length > 0 && (quelle === 'text' ? text.trim().length > 0 : datei !== null);

  async function starten(): Promise<void> {
    setFehler(null);
    setLaeuft(true);
    try {
      const neu =
        quelle === 'datei' && datei
          ? await rueckfuehrenDatei(datei, vaultId)
          : await rueckfuehrenText(text, vaultId);
      // Eine alte Blob-URL (Dateiergebnis) lebt nur im Browser-Speicher — wird
      // sie hier durch ein neues Ergebnis ersetzt, muss sie freigegeben werden.
      gibErgebnisFrei(ergebnis);
      setErgebnis(neu);
    } catch (e) {
      // Ein fehlgeschlagener Lauf verwirft ein evtl. noch vorhandenes altes
      // Dateiergebnis — dessen Blob-URL muss vor dem Verwerfen freigegeben
      // werden, sonst bleibt sie bis zum Reload im Speicher hängen.
      gibErgebnisFrei(ergebnis);
      setFehler(fehlerMeldung(e));
      setErgebnis(null);
    } finally {
      setLaeuft(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="space-y-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Rückführen</h2>

        <Hinweis ton="warnung">
          <p className="font-medium">Achtung: Hier entstehen wieder echte Personendaten.</p>
          <p className="mt-1">
            Die Rückführung ersetzt die Pseudonyme durch den gespeicherten Klartext. Das Ergebnis
            ist ein Dokument mit unmittelbar identifizierbaren Personen. Behandeln Sie es
            entsprechend: nicht weitergeben, nicht in unverschlüsselten Ablagen speichern, nach
            Gebrauch löschen.
          </p>
        </Hinweis>

        {/* Vault-Auswahl */}
        <div>
          <div className="flex items-center justify-between gap-3 mb-1">
            <label
              htmlFor="vault"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Zuordnungstabelle (Vault) *
            </label>
            <button
              type="button"
              onClick={() => void vaultsLaden_()}
              className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              Neu laden
            </button>
          </div>
          <select
            id="vault"
            value={vaultId}
            onChange={(e) => setVaultId(e.target.value)}
            disabled={vaultsLaden}
            className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          >
            <option value="">— bitte wählen —</option>
            {vaults.map((v) => (
              <option key={v.id} value={v.id}>
                {vaultBeschriftung(v)}
              </option>
            ))}
          </select>
          {vaultsLaden && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Zuordnungstabellen werden geladen …
            </p>
          )}
          {!vaultsLaden && !vaultsFehler && vaults.length === 0 && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Es sind noch keine Zuordnungstabellen vorhanden. Sie entstehen beim Pseudonymisieren
              im Modus „umkehrbar“.
            </p>
          )}
        </div>

        {vaultsFehler && (
          <Hinweis ton="fehler">
            Die Zuordnungstabellen konnten nicht geladen werden: {vaultsFehler}
          </Hinweis>
        )}

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
              htmlFor="pseudonymtext"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Pseudonymisierter Text
            </label>
            <textarea
              id="pseudonymtext"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={10}
              placeholder="Text mit Pseudonymen der Form [[PERSON_1]] einfügen."
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        ) : (
          <DateiFeld datei={datei} onDatei={setDatei} onFehler={setFehler} deaktiviert={laeuft} />
        )}
      </section>

      {fehler && <Hinweis ton="fehler">{fehler}</Hinweis>}

      <button
        type="button"
        onClick={() => void starten()}
        disabled={!kannStarten || laeuft}
        className="inline-flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 transition-colors disabled:opacity-50"
      >
        {laeuft && <Loader2 className="w-4 h-4 animate-spin" />}
        {laeuft ? 'Wird zurückgeführt …' : 'Zurückführen'}
      </button>

      {ergebnis && (
        <div className="border-t border-gray-200 dark:border-gray-700 pt-6">
          <ErgebnisFeld
            titel="Klartext"
            text={ergebnis.text}
            downloadUrl={ergebnis.downloadUrl}
            dateiname={ergebnis.dateiname}
            fussnote="Dieses Ergebnis enthält Personendaten im Klartext."
          />
        </div>
      )}
    </div>
  );
}
