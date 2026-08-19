// Status eines API-Schlüssels — abgeleitet, nicht gespeichert.
//
// Bewusst hier und nicht im Panel: sowohl die Schlüsseltabelle als auch die
// Nutzungsauswertung zeigen denselben Badge. Zwei Kopien liefen bei der ersten
// Farbänderung auseinander.

/** Was von einem Schlüssel für die Statusableitung gebraucht wird. */
export interface StatusQuelle {
  revokedAt: string | null;
  expiresAt: string | null;
}

export type SchluesselStatus = 'aktiv' | 'widerrufen' | 'abgelaufen';

/**
 * Status wird bewusst im Frontend abgeleitet statt nur `aktiv` aus der API zu
 * übernehmen: „abgelaufen“ ist ein reiner Zeitvergleich (`expiresAt` in der
 * Vergangenheit, nicht widerrufen) und muss sich beim blossen Neuladen der
 * Seite aktualisieren, ohne dass der Server dafür etwas Neues schreibt.
 */
export function schluesselStatus(s: StatusQuelle): SchluesselStatus {
  if (s.revokedAt) return 'widerrufen';
  if (s.expiresAt && new Date(s.expiresAt).getTime() < Date.now()) return 'abgelaufen';
  return 'aktiv';
}

export const STATUS_LABEL: Record<SchluesselStatus, string> = {
  aktiv: 'Aktiv',
  widerrufen: 'Widerrufen',
  abgelaufen: 'Abgelaufen',
};

// Farben immer per Record nachschlagen, nie inline.
const STATUS_FARBE: Record<SchluesselStatus, string> = {
  aktiv: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  widerrufen: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  abgelaufen: 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400',
};

/** Farbiges Status-Abzeichen eines Schlüssels. */
export default function StatusBadge({ status }: { status: SchluesselStatus }) {
  return (
    <span
      className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_FARBE[status]}`}
    >
      {STATUS_LABEL[status]}
    </span>
  );
}
