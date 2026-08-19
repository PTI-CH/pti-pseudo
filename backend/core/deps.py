"""Gemeinsame FastAPI-Abhaengigkeiten: Datenbanksitzung, Mandant, Benutzer.

Zur Mandantenfaehigkeit: das Fachkonzept sieht echte Mehrmandantenfaehigkeit
mit API-Schluesseln vor (Abschnitt 8), aber erst ab Phase 2. In Phase 1 gibt es
genau einen Mandanten, und alle Tabellen tragen trotzdem schon ``tenant_id``.
Das ist Absicht: die Spalte nachtraeglich einzuziehen waere eine Migration ueber
den gesamten Vault-Bestand, sie jetzt mitzufuehren kostet nichts.

Zur Laenge von ``api_keys.label``: die 140-Zeichen-Grenze am Verwaltungs-Endpunkt
stammt aus der Zeit, als ``current_actor`` die Bezeichnung in
``usage_events.username`` (``String(150)``) schrieb; seit der Attribution ueber
die Schluessel-ID ist dort die Laenge fest und die Grenze nur noch eine
harmlose Obergrenze fuer die Anzeige — sie bleibt deshalb stehen.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import get_sessionmaker
from backend.vault.models import Tenant

#: Name des Phase-1-Mandanten. Wird beim ersten Zugriff angelegt.
DEFAULT_TENANT_NAME = "PTI"


def get_session() -> Iterator[Session]:
    """Datenbanksitzung je Request, die am Ende zuverlaessig geschlossen wird."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_tenant(session: Session, request: Request | None = None) -> Tenant:
    """Liefert den Mandanten dieses Requests.

    Zwei Herkuenfte, eine Funktion:

    * **Maschinenpfad** (``/api/ext/v1``) — der API-Schluessel traegt seine
      ``tenant_id``; sie gilt. In Phase 1 ist das immer derselbe PTI-Mandant,
      aber der Weg ueber den Schluessel steht schon, damit Phase 2 die
      Anwendungen ohne Schemaumbau trennen kann.
    * **Menschenpfad** (``/api/v1``) — der Default-Mandant, beim ersten Zugriff
      angelegt.

    ``request`` ist optional, damit Aufrufer ohne Request-Bezug (Werkzeuge,
    Tests, Aufraeumlaeufe) unveraendert weiterarbeiten koennen.
    """
    api_key = getattr(request.state, "api_key", None) if request is not None else None
    if api_key is not None:
        tenant = session.get(Tenant, api_key.tenant_id)
        if tenant is None:
            # Bewusst ein Fehler und KEIN Rueckfall auf den Default-Mandanten:
            # ein Schluessel, dessen Mandant verschwunden ist, wuerde sonst
            # still in einem fremden Bestand arbeiten — Vaults dort anlegen,
            # Begriffe dort aendern. Lieber ein lautes 500, das jemand
            # bemerkt, als eine leise Vermischung, die niemand bemerkt.
            raise RuntimeError(
                "Mandant des API-Schluessels nicht gefunden "
                f"(Schluessel {api_key.key_prefix})."
            )
        return tenant

    tenant = session.execute(
        select(Tenant).where(Tenant.name == DEFAULT_TENANT_NAME)
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=DEFAULT_TENANT_NAME)
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
    return tenant


def current_user(request: Request) -> dict:
    """Der vom SSO-Gate hinterlegte Benutzer.

    Das Gate laeuft als Middleware vor jeder gegateten Route und setzt
    ``request.state.hub_user``. Ist dort nichts, wurde die Route faelschlich
    ausgenommen — dann lieber ein leeres Profil als ein Absturz, denn der
    Zugriffsschutz haengt nicht an dieser Funktion, sondern am Gate.
    """
    user = getattr(request.state, "hub_user", None)
    return user if isinstance(user, dict) else {}


def current_actor(request: Request) -> str:
    """Wer diesen Request ausgeloest hat — Mensch oder Anwendung.

    Der einheitliche Aktor-Begriff beider Pfade (Design-Spec Abschnitt 4.4):

    * SSO-Pfad: der Hub-Benutzername. Bewusst ``username`` und nicht die UUID —
      ``user.person`` kann im CRM ``null`` sein, und ein Top-Level-``email``
      gibt es dort nicht.
    * API-Pfad: ``apikey:<Schluessel-ID>``. Das Praefix macht in der
      Nutzungserfassung auf einen Blick sichtbar, dass hier keine Person
      gearbeitet hat, sondern eine andere PTI-Anwendung.

    Warum die **ID** und nicht die Bezeichnung: Bezeichnungen sind nicht
    eindeutig. Ein widerrufener Schluessel wird gern unter demselben Namen neu
    ausgestellt — eine Auswertung nach Bezeichnung wuerde die Zahlen beider
    vermengen und dem neuen Schluessel die Aufrufe des alten zuschreiben. Die
    ID ist eindeutig und unveraenderlich; die Bezeichnung setzt die Auswertung
    beim Anzeigen ueber den Join wieder daran (siehe
    ``routers/api_keys.py``). Der Preis ist, dass ein roher Blick in
    ``usage_events`` nur noch eine UUID zeigt — das ist der Tabelle, die
    ausdruecklich nur Metadaten traegt, zumutbar.
    """
    api_key = getattr(request.state, "api_key", None)
    if api_key is not None:
        return f"apikey:{api_key.id}"
    return str(current_user(request).get("username") or "unbekannt")


def current_username(request: Request) -> str:
    """Aktor fuer die Nutzungserfassung — siehe ``current_actor``.

    Der Name ist historisch und passt zur Spalte ``usage_events.username``;
    seit dem Maschinenpfad steht dort nicht mehr zwingend ein Mensch.
    """
    return current_actor(request)
