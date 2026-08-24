import { useRef, useState, type DragEvent } from 'react';
import { FileText, Upload, X } from 'lucide-react';

/**
 * Formate, die die Engine liest.
 *
 * Muss zu `supported_formats()` im Backend passen (`backend/adapters/__init__.py`).
 * Steht hier zusätzlich, damit eine unpassende Datei schon beim Ablegen
 * abgewiesen wird und nicht erst nach dem Hochladen.
 */
export const ERLAUBTE_ENDUNGEN = ['.docx', '.xlsx', '.pptx', '.txt', '.csv'] as const;
const ACCEPT = ERLAUBTE_ENDUNGEN.join(',');

interface Props {
  datei: File | null;
  onDatei: (datei: File | null) => void;
  /** Wird gesetzt, wenn eine Datei mit unpassender Endung fallen gelassen wird. */
  onFehler?: (meldung: string) => void;
  deaktiviert?: boolean;
}

function endungErlaubt(name: string): boolean {
  const kleingeschrieben = name.toLowerCase();
  return ERLAUBTE_ENDUNGEN.some((e) => kleingeschrieben.endsWith(e));
}

function groesseLesbar(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Ablagefläche mit Drag & Drop und klassischer Dateiauswahl. */
export default function DateiFeld({ datei, onDatei, onFehler, deaktiviert = false }: Props) {
  const [ueberFlaeche, setUeberFlaeche] = useState(false);
  const eingabeRef = useRef<HTMLInputElement>(null);

  function pruefeUndUebernimm(kandidat: File | undefined): void {
    if (!kandidat) return;
    if (!endungErlaubt(kandidat.name)) {
      onFehler?.(
        `Dieses Format wird nicht unterstützt. Erlaubt sind ${ERLAUBTE_ENDUNGEN.join(', ')}.`,
      );
      return;
    }
    onDatei(kandidat);
  }

  function beiAblage(e: DragEvent<HTMLDivElement>): void {
    e.preventDefault();
    setUeberFlaeche(false);
    if (deaktiviert) return;
    pruefeUndUebernimm(e.dataTransfer.files?.[0]);
  }

  if (datei) {
    return (
      <div className="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
        <FileText className="w-5 h-5 shrink-0 text-blue-600 dark:text-blue-400" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
            {datei.name}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400">{groesseLesbar(datei.size)}</p>
        </div>
        <button
          type="button"
          onClick={() => onDatei(null)}
          disabled={deaktiviert}
          className="p-1.5 rounded-full text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
          aria-label="Datei entfernen"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
    );
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!deaktiviert) setUeberFlaeche(true);
      }}
      onDragLeave={() => setUeberFlaeche(false)}
      onDrop={beiAblage}
      onClick={() => !deaktiviert && eingabeRef.current?.click()}
      className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors ${
        deaktiviert
          ? 'cursor-not-allowed opacity-50 border-gray-300 dark:border-gray-600'
          : 'cursor-pointer'
      } ${
        ueberFlaeche
          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
          : 'border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50'
      }`}
    >
      <Upload className="w-6 h-6 text-gray-400 dark:text-gray-500" />
      <p className="text-sm text-gray-700 dark:text-gray-300">
        Datei hierher ziehen oder <span className="text-blue-600 dark:text-blue-400">auswählen</span>
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Unterstützt werden {ERLAUBTE_ENDUNGEN.join(', ')}
      </p>
      <input
        ref={eingabeRef}
        type="file"
        accept={ACCEPT}
        className="hidden"
        onChange={(e) => {
          pruefeUndUebernimm(e.target.files?.[0]);
          // Zurücksetzen, damit dieselbe Datei erneut gewählt werden kann.
          e.target.value = '';
        }}
      />
    </div>
  );
}
