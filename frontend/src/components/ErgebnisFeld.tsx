import { useState } from 'react';
import { Check, Copy, Download } from 'lucide-react';

interface Props {
  titel: string;
  text: string | null;
  /** Bei Dateiverarbeitung: Link auf das fertige Dokument. */
  downloadUrl?: string | null;
  dateiname?: string | null;
  /** Zusätzliche Warnung über dem Feld (z. B. beim Rückführen). */
  fussnote?: string;
}

/** Ausgabefeld für den verarbeiteten Text, mit Kopieren und Herunterladen. */
export default function ErgebnisFeld({ titel, text, downloadUrl, dateiname, fussnote }: Props) {
  const [kopiert, setKopiert] = useState(false);

  async function kopieren(): Promise<void> {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setKopiert(true);
      window.setTimeout(() => setKopiert(false), 1500);
    } catch {
      // Zwischenablage kann vom Browser gesperrt sein — kein Grund für einen
      // Fehlerdialog, der Text steht ja im Feld und lässt sich markieren.
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">{titel}</h3>
        <div className="flex items-center gap-3">
          {text && (
            <button
              type="button"
              onClick={() => void kopieren()}
              className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
            >
              {kopiert ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
              {kopiert ? 'Kopiert' : 'Kopieren'}
            </button>
          )}
          {downloadUrl && (
            <a
              href={downloadUrl}
              download={dateiname ?? undefined}
              className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
            >
              <Download className="w-3.5 h-3.5" />
              {dateiname ? `${dateiname} herunterladen` : 'Herunterladen'}
            </a>
          )}
        </div>
      </div>

      {text ? (
        <pre className="w-full max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-100 px-3 py-2 text-sm font-mono">
          {text}
        </pre>
      ) : (
        <p className="rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-6 text-center text-sm text-gray-400 dark:text-gray-500">
          Das Ergebnis liegt als Datei vor — bitte oben herunterladen.
        </p>
      )}

      {fussnote && <p className="text-xs text-gray-500 dark:text-gray-400">{fussnote}</p>}
    </div>
  );
}
