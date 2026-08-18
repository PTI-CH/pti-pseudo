// Wer ist angemeldet — speist die Anzeige in der Kopfzeile.
//
// WICHTIG: reine Kosmetik. Die tatsächliche Zugriffskontrolle sitzt im
// TIP-Hub-SSO-Gate des Backends. Schlägt `/api/me` fehl (Backend noch nicht da,
// SSO lokal auf `off`, 401), bleibt es beim anonymen Zustand und die Kopfzeile
// rendert einfach ohne Benutzer — nie ein Fehler auf der ganzen Seite.

import { useEffect, useState } from 'react';

export interface Benutzer {
  username: string | null;
  name: string | null;
  role: string | null;
  isAdmin: boolean;
}

const ANONYM: Benutzer = { username: null, name: null, role: null, isAdmin: false };

async function ladeBenutzer(): Promise<Benutzer> {
  const res = await fetch('/api/me');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const daten = (await res.json()) as Partial<Benutzer> | null;
  return {
    username: daten?.username ?? null,
    name: daten?.name ?? null,
    role: daten?.role ?? null,
    isAdmin: daten?.isAdmin === true,
  };
}

export function useBenutzer(): Benutzer {
  const [benutzer, setBenutzer] = useState<Benutzer>(ANONYM);

  useEffect(() => {
    let abgemeldet = false;
    void (async () => {
      try {
        const b = await ladeBenutzer();
        if (!abgemeldet) setBenutzer(b);
      } catch {
        // Bewusst still: ein nicht erreichbarer Endpunkt darf die Oberfläche
        // nicht blockieren.
      }
    })();
    return () => {
      abgemeldet = true;
    };
  }, []);

  return benutzer;
}

/** Anzeigename: bevorzugt der volle Name, sonst der Benutzername. */
export function anzeigeName(b: Benutzer): string | null {
  return b.name || b.username || null;
}

/** Deutsche Rollenbezeichnung. Unbekanntes wird roh durchgereicht. */
const ROLLEN_LABEL: Record<string, string> = {
  admin: 'Administrator',
  standard: 'Standardbenutzer',
  readonly: 'Nur Lesen',
};

export function rollenLabel(rolle: string | null): string | null {
  if (!rolle) return null;
  return ROLLEN_LABEL[rolle] ?? rolle;
}
