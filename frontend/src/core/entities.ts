// Zentrale Uebersetzungstabelle fuer die Entitaetstypen der Engine.
//
// Presidio liefert englische Bezeichner (PERSON, IBAN_CODE, …), die Oberflaeche
// zeigt ausschliesslich deutsche Labels. Diese Map ist die EINZIGE Stelle, an
// der uebersetzt wird — neue Erkenner werden hier ergaenzt.

/** Die Typen, die Engine und eigene Schweizer Erkenner heute liefern koennen. */
export const ENTITAETS_TYPEN = [
  'PERSON',
  'LOCATION',
  'ORGANIZATION',
  'EMAIL_ADDRESS',
  'IBAN_CODE',
  'DATE_TIME',
  'URL',
  'IP_ADDRESS',
  'CREDIT_CARD',
  'CH_AHV',
  'CH_IBAN',
  'CH_PHONE',
  'CH_POSTAL_CODE',
  'CH_PLATE',
] as const;

export type EntitaetsTyp = (typeof ENTITAETS_TYPEN)[number];

export const ENTITAETS_LABEL: Record<EntitaetsTyp, string> = {
  PERSON: 'Person',
  LOCATION: 'Ort',
  ORGANIZATION: 'Organisation',
  EMAIL_ADDRESS: 'E-Mail-Adresse',
  IBAN_CODE: 'IBAN',
  DATE_TIME: 'Datum / Zeit',
  URL: 'Internetadresse',
  IP_ADDRESS: 'IP-Adresse',
  CREDIT_CARD: 'Kreditkarte',
  CH_AHV: 'AHV-Nummer',
  CH_IBAN: 'IBAN (Schweiz)',
  CH_PHONE: 'Telefonnummer (Schweiz)',
  CH_POSTAL_CODE: 'Postleitzahl (Schweiz)',
  CH_PLATE: 'Autokennzeichen (Schweiz)',
};

/**
 * Deutsches Label zu einem Typ. Unbekannte Typen (z. B. ein neuer Erkenner im
 * Backend, den die Oberflaeche noch nicht kennt) werden nicht verschluckt,
 * sondern roh durchgereicht — besser ein englisches Wort als ein leeres Feld.
 */
export function labelFuerTyp(typ: string): string {
  return ENTITAETS_LABEL[typ as EntitaetsTyp] ?? typ;
}

// ─── Verlaesslichkeits-Ampel ────────────────────────────────────────────────
//
// Der Zweck der Befund-Ansicht: die gelben und roten Funde sind die, die ein
// Mensch pruefen muss. Gruen darf ueberflogen werden.

export type AmpelStufe = 'gruen' | 'gelb' | 'rot';

export function ampelStufe(konfidenz: number): AmpelStufe {
  if (konfidenz >= 0.85) return 'gruen';
  if (konfidenz >= 0.5) return 'gelb';
  return 'rot';
}

/** Badge-Farben je Ampelstufe — immer per Record nachschlagen, nie inline. */
export const AMPEL_FARBE: Record<AmpelStufe, string> = {
  gruen: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  gelb: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
  rot: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
};

/** Farbe des reinen Ampelpunkts (ohne Hintergrundflaeche). */
export const AMPEL_PUNKT: Record<AmpelStufe, string> = {
  gruen: 'bg-green-500 dark:bg-green-400',
  gelb: 'bg-yellow-500 dark:bg-yellow-400',
  rot: 'bg-red-500 dark:bg-red-400',
};

export const AMPEL_TEXT: Record<AmpelStufe, string> = {
  gruen: 'sicher',
  gelb: 'zu prüfen',
  rot: 'unsicher',
};

/** Konfidenz als Prozentwert fuer die Anzeige, z. B. 0.8342 → "83 %". */
export function konfidenzProzent(konfidenz: number): string {
  return `${Math.round(konfidenz * 100)} %`;
}
