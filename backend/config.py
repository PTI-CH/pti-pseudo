"""Zentrale Konfiguration des PTI-Pseudonymisierers.

Alle Werte stammen aus Umgebungsvariablen; die verbindliche Liste steht in
``.env.example`` im Repo-Root.

Zur Herkunft der Werte:

* **Produktion (Infomaniak jCloud):** Die Variablen kommen aus dem
  Jelastic-Dashboard. Nach jeder Aenderung dort ist ein *Dashboard*-Restart
  noetig — ein blosser Prozess-Restart uebernimmt sie nicht.
* **Lokal:** ueber eine ``.env``-Datei, deren Pfad in ``ENV_FILE`` steht.

``ENV_FILE`` ist bewusst der einzige Weg, eine Datei zu laden: Presidios eigene
``.env`` liegt ebenfalls im Repo-Root, ist upstream getrackt und gehoert nicht
uns. Ein implizites Laden von ``./.env`` wuerde fremde Werte einziehen. In
Produktion liegt die Datei zudem *ausserhalb* des Deploy-Roots, damit sie
VCS-Updates ueberlebt.
"""

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typisierte Sicht auf die Umgebungsvariablen.

    Feldnamen entsprechen den Variablennamen in Kleinschreibung
    (``crm_base_url`` -> ``CRM_BASE_URL``).
    """

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Fremde Variablen (Presidio, Docker-Compose, Jelastic) duerfen im
        # Prozess stehen, ohne die Validierung zu sprengen.
        extra="ignore",
    )

    # ── Laufzeit ───────────────────────────────────────────────────────────
    # Steuert den HTTPS-Redirect. Default "production", damit ein vergessenes
    # ENV nicht versehentlich die Umleitung abschaltet (fail secure).
    env: str = "production"
    port: int = 8000

    # ── Datenbank ──────────────────────────────────────────────────────────
    # Eigene DB, bewusst nicht die geteilte pti-db: Vault-Ciphertext gehoert
    # nicht in die CRM-Datenbank.
    database_url: str = ""

    # ── TIP-Hub SSO (Ansatz B) ─────────────────────────────────────────────
    crm_base_url: str = "https://crm.pti-tip-hub.ch"
    hub_base_url: str = "https://pti-tip-hub.ch"
    # Geprueftes Flag aus ``session.apps``; muss zur CRM-Spalte passen.
    hub_app_key: str = "pseudonymizer"
    # Rate-Limit-Bypass beim CRM, KEIN Auth-Faktor.
    relay_secret: str = ""
    public_base_url: str = "https://pseudo.pti-tip-hub.ch"

    # Lokale Entwicklung ohne CRM: ``HUB_AUTH=off`` schaltet das Gate ab.
    # Optional, damit "nicht gesetzt" von "auf etwas anderes gesetzt"
    # unterscheidbar bleibt.
    hub_auth: str | None = None
    # Rolle des synthetischen Dev-Users. Leer => das Gate setzt "admin".
    dev_role: str = ""

    # ── Vault ──────────────────────────────────────────────────────────────
    # 32 Byte hex. VERLUST = ALLE VAULTS UNWIEDERBRINGLICH UNLESBAR.
    vault_master_key: str = ""

    # ── Engine ─────────────────────────────────────────────────────────────
    # Muss zum Modell-Wheel in pti-requirements.txt passen.
    spacy_model: str = "de_core_news_md"
    llm_provider: str = "none"
    infomaniak_ai_key: str | None = None

    # ── Grenzen ────────────────────────────────────────────────────────────
    # Muss zu client_max_body_size im NGINX des Knotens passen.
    max_upload_bytes: int = 26_214_400  # 25 MiB
    # Ab dieser Groesse nicht mehr synchron verarbeiten (Gateway-Timeout).
    sync_threshold_bytes: int = 2_097_152
    # Obergrenze fuer den JSON-Textpfad (documents:detect / :anonymize).
    # ``spaCy`` bricht bei ``nlp.max_length`` (Default 1'000'000 Zeichen) mit
    # einem rohen ValueError ab, sobald ein Text laenger ist — anders als beim
    # Datei-Upload gibt es fuer diesen Pfad sonst keine Vor-Pruefung. Der Wert
    # hier liegt bewusst deutlich darunter, damit die Anfrage sauber mit 413
    # abgelehnt wird, statt die Engine mitten in der Verarbeitung abstuerzen
    # zu lassen.
    max_text_chars: int = 300_000

    @field_validator("crm_base_url", "hub_base_url", "public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Entfernt abschliessende Schraegstriche aus Basis-URLs.

        Ohne das entstehen beim Zusammensetzen doppelte Slashes
        (``https://crm.example.ch//api/auth/session``) und Redirect-Ziele, die
        der Hub nicht wiedererkennt.
        """
        return value.rstrip("/") if isinstance(value, str) else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Liefert die prozessweit einmalig geparsten Settings.

    Der Cache spart das wiederholte Parsen pro Request. ``ENV_FILE`` wird hier
    (und nicht im ``model_config``) ausgewertet, damit der Pfad zur Laufzeit
    gilt und nicht schon beim Import eingefroren wird.

    Returns
    -------
    Settings
        Die validierte Konfiguration.
    """
    env_file = os.getenv("ENV_FILE") or None
    return Settings(_env_file=env_file)
