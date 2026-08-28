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

    # Ab welcher Stufe die Anwendung protokolliert.
    #
    # Vorgabe INFO, und das ist eine Korrektur: Bis zum 2026-08-25 konfigurierte
    # die Anwendung ihr Protokoll GAR NICHT. `pti.*` fiel damit auf den
    # Wurzel-Logger zurueck, dessen Vorgabe WARNING ist — auf dem Knoten
    # nachgemessen stand `logger.warning("Schema ergaenzt…")` im Protokoll,
    # `logger.info("Erkennungs-Engine geladen…")` null Mal.
    #
    # Das kostete echte Daten: Die Messung zu Ort und Organisation
    # (`pipeline._ki_messen_ort_organisation`) schreibt mit `logger.info` und
    # sammelte seit ihrem Deploy nichts, obwohl sie ausdruecklich als
    # „erst mitmessen, dann entscheiden" gebaut wurde.
    #
    # Das Volumen traegt: Am 2026-08-26 gemessen bestand das Protokoll des
    # Knotens zur Haelfte aus `GET /api/health` des Balancers (10 388 von
    # 20 191 Zeilen in acht Tagen). Unsere INFO-Zeilen sind dagegen eine
    # Handvoll je verarbeitetem Dokument.
    log_level: str = "INFO"

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

    # ── KI-Schicht (Phase 3, Variante C: Infomaniak AI Tools) ──────────────
    # Noch wertet kein Code diese Einstellungen aus. Sie stehen hier, damit
    # der Schluessel einen dokumentierten Platz hat und die Namen zu
    # `pti-rag` passen — dort laeuft die Anbindung bereits, und zwei
    # Schreibweisen fuer dieselbe Sache waeren beim Uebertragen die erste
    # Fehlerquelle.
    #
    # WICHTIG: Der Schluessel allein genuegt nicht. Die Produkt-Id steht im
    # **Pfad** der Anfrage:
    #     {base}/2/ai/{product_id}/openai/v1/chat/completions
    # Ohne sie laesst sich der Dienst nicht einmal adressieren; der Bearer-
    # Token authentisiert nur.
    infomaniak_ai_key: str | None = None
    infomaniak_ai_product_id: str = ""
    infomaniak_ai_base: str = "https://api.infomaniak.com"
    # Apertus, und das ist eine MESSUNG, keine Uebernahme.
    #
    # Zwischenzeitlich stand hier `Ministral-3`, weil es fuer
    # Massenklassifikation empfohlen ist. An unserer Aufgabe gemessen
    # (2026-08-24, Golden-Set) faellt es aber aus: Bei drei von neun
    # Dokumenten gibt es eine oeffnende Klammer aus und danach 2000 Token
    # Leerzeichen (`finish_reason: length`) — die Antwort ist damit kein
    # gueltiges JSON. Reproduzierbar, und weder von `temperature: 0.2` noch
    # von `additionalProperties: false` behoben:
    #
    #     Ministral-3  temp 0.0   0/3 brauchbar
    #     Ministral-3  temp 0.2   0/3 brauchbar
    #     Apertus      temp 0.0   3/3 brauchbar
    #     gemma-4      temp 0.0   3/3 brauchbar
    #
    # Ueber das ganze Golden-Set: Apertus 9/9 Anfragen gelungen, 0.7 bis
    # 2.2 s je Anfrage. `google/gemma-4-31B-it` waere die Ausweichwahl.
    infomaniak_ai_modell: str = "swiss-ai/Apertus-v1.5-70B"

    # Betriebsschalter fuer die Zweitmeinung (Stufe D, backend/ki/zweitmeinung.py).
    #
    # Sie ist die einzige Stufe, bei der der VOLLSTAENDIGE Dokumenttext zur KI
    # geht — maskiert, also ohne jeden erkannten Namen, aber mit dem gesamten
    # Sachverhalt. Bei einem Polizeidokument kann der so heikel sein wie die
    # Namen.
    #
    # Rechtlich ist das getragen: Der Auftraggeber hat am 2026-08-25 bestaetigt,
    # dass der jCloud-Auftragsverarbeitungsvertrag die AI Tools **mit umfasst**.
    # Der Schalter ist damit kein Notaus mehr, sondern eine reine
    # Betriebsentscheidung — die Begruendung fuer die Vorgabe steht unten und
    # ist eine gemessene, keine rechtliche.
    #
    # `KI_ZWEITMEINUNG=off` im Infomaniak-Dashboard
    # (danach Dashboard-Restart, ein Prozess-Restart laedt Variablen nicht neu)
    # stellt die Stufe ab, ohne dass jemand Code anfassen oder deployen muss.
    #
    # Er ist die BETRIEBS-Entscheidung; `ki_pruefung` je Lauf bleibt die
    # Entscheidung des Nutzers. Beide muessen zutreffen.
    #
    # VORGABE AUS, und das ist gemessen und nicht vorsichtshalber. Am
    # 2026-08-25 am Golden-Set gegen den echten Dienst:
    #
    #     ohne Zweitmeinung   93.2 % Precision, 100 % Recall, PERSON 100/100
    #     mit  Zweitmeinung   87.2 % Precision,  98.6 % Recall, PERSON 84/96
    #
    # Die Stufe findet nicht das Falsche — sie zerstoert das Richtige. Sie
    # verdreifacht die Anfragen je Dokument (Namenspruefung, Ort/Organisation,
    # Zweitmeinung); der Dienst antwortet unter dieser Last mit Muell statt
    # mit einem sauberen 429, und die NAMENSPRUEFUNG faellt aus. Im letzten
    # Lauf 26-mal — dieselbe Pruefung, die ohne die Zweitmeinung 100 %
    # erreicht. `Sandra Zbinden` blieb dadurch zerrissen.
    #
    # Die Stufe ist damit nicht schlecht gebaut, sondern zu teuer fuer das
    # Kontingent. Sie bleibt vollstaendig, getestet und dokumentiert im Code
    # und laesst sich mit `KI_ZWEITMEINUNG=on` im Dashboard zuschalten —
    # sinnvoll erst, wenn die Anfragezahl gesenkt ist (ueberlappende Fenster
    # zusammenfassen) oder Infomaniak das Kontingent anhebt.
    ki_zweitmeinung: bool = False

    # ── OCR (Phase 6 / Zyklus B, Stufe 2) ───────────────────────────────────
    # Ob die Tesseract-OCR-Stufe fuer gescannte PDF-Seiten laufen darf.
    #
    # VORGABE AUS, und das ist eine DEPLOY-Entscheidung, keine Vorsicht auf
    # Zuruf. ``pytesseract`` (in ``pti-requirements.txt``) ist nur die duenne
    # Python-Anbindung; das eigentliche Binary ``tesseract`` samt den
    # Sprachdaten ``deu`` ist ein SYSTEMBINARY, das auf dem Jelastic-Knoten
    # installiert sein muss -- ``pip install`` erreicht es nicht, und der
    # Benutzer ``python`` hat dort kein ``sudo``. Ohne dieses Binary wirft
    # jeder OCR-Versuch, egal was hier steht (siehe ``backend/ocr``).
    # Der Schalter existiert deshalb, damit ein Knoten ohne installiertes
    # Binary nicht bei jeder gescannten Seite einen Absturz statt einer
    # klaren Meldung produziert -- siehe Spec §4.3 (die tragende
    # Sicherheitsregel): Ein Scan, den niemand lesen konnte, scheitert LAUT
    # ueber ``NutzermeldungError``, nie still.
    # ``OCR_ENABLED=on`` im Jelastic-Dashboard (danach Dashboard-Restart)
    # schaltet die Stufe zu, sobald das Binary auf dem Knoten liegt.
    # Details: ``docs/superpowers/specs/2026-08-28-pdf-ocr-design.md``, §2/§4.
    ocr_enabled: bool = False
    # Rasterungs-Aufloesung (dpi) fuer ``page.get_pixmap(dpi=...)`` vor dem
    # OCR-Lauf. Der Hebel fuer den Speicherverbrauch: Eine hoehere Aufloesung
    # verbessert die Erkennung kaum noch, verdoppelt aber den Pixelspeicher
    # je Verdopplung -- Spec §4.4 misst rund 1.1 GB Spitze je OCR-Lauf bei
    # diesem Wert, und der Auftrags-Arbeiter faehrt bewusst nur EINEN Job je
    # Worker-Prozess (siehe ``backend/jobs/arbeiter.py``), genau damit ein
    # solcher Ausschlag nicht mit einem zweiten Lauf im selben Prozess
    # kollidiert. 200 dpi ist der fuer Tesseract uebliche, massvolle Wert.
    ocr_dpi: int = 200

    # ── Grenzen ────────────────────────────────────────────────────────────
    # Muss zu client_max_body_size im NGINX des Knotens passen.
    max_upload_bytes: int = 26_214_400  # 25 MiB
    # ── Wann eine Datei zum Auftrag wird ───────────────────────────────────
    #
    # ZWEI Schranken, und die Reihenfolge ist der Punkt: erst die Bytes (grob,
    # ohne die Datei zu oeffnen), dann die Zeichen (scharf, nach dem
    # Adapterlauf). Die Entscheidung faellt in
    # ``documents._laeuft_als_auftrag``.
    #
    # Der Grund fuer die zweite Schranke ist gemessen (2026-08-28,
    # ``scripts/schwelle_messen.py``): Die Verarbeitungsdauer ist linear im
    # TEXT -- rund 20'000 Zeichen je Sekunde, ueber alle Formate hinweg --,
    # und die Dateigroesse sagt darueber fast nichts. Dieselben 64'107
    # Zeichen wiegen als txt 64 KiB, als docx 39 KiB und als xlsx 8 KiB;
    # Faktor acht. Eine Byte-Schranke allein war deshalb entweder zu hoch
    # (die 2 MiB hier waren bei xlsx-Dichte ueber 17 Millionen Zeichen,
    # also rund 15 Minuten in einem Request, den der Balancer nach einer
    # Minute abschneidet) oder so tief, dass sie jedes normale Dokument in
    # den Auftragspfad gedraengt haette.
    #
    # Grobe Vorpruefung, ohne die Datei zu oeffnen. Sie bleibt noetig: Eine
    # sehr grosse Datei soll gar nicht erst durch den Adapter gehen, und fuer
    # eine beschaedigte Datei ist sie die einzige Schranke, die ueberhaupt
    # greift.
    sync_threshold_bytes: int = 2_097_152
    # Scharfe Pruefung, nach dem Adapterlauf.
    #
    # Das Zeitbudget ist seit dem 2026-08-28 GEMESSEN und betraegt **300 s**,
    # nicht 60. Ein Lauf ueber 280'000 Zeichen bekam nach exakt 300.5 s einen
    # sauberen ``504`` vom Balancer; Laeufe von 63 bis 127 s gingen mehrfach
    # glatt durch. Der Wert ist also rund fuenfmal so gross wie die frueher
    # hier angenommene ``proxy_read_timeout``-Vorgabe.
    #
    # Daraus abgeleitet, am 2026-08-28: **400'000 Zeichen**.
    #
    # Die Rechnung, bewusst mit dem SCHLECHTESTEN gemessenen Durchsatz und
    # nicht mit dem Mittelwert:
    #
    #   langsamster Lauf   270'000 Zeichen in 127 s  ->  ~2'130 Zeichen/s
    #   400'000 Zeichen    bei diesem Tempo          ->  ~188 s
    #   Abstand zur Grenze 300 s - 188 s             ->  ~112 s Reserve
    #
    # Warum nicht mehr: Bei 500'000 waeren es im langsamsten Fall rund 235 s
    # und die Reserve schruempfte auf gut eine Minute -- zu wenig, sobald ein
    # zweiter grosser Lauf danebenlaeuft oder der Upload selbst Zeit kostet
    # (der zaehlt mit, die Balancer-Uhr laeuft ab Verbindungsaufbau).
    #
    # Diese Zahl haengt an ``WORKERS`` in ``deploy/pythonenv``: Die Streuung
    # um Faktor zwei kommt aus der Konkurrenz gleichzeitiger Laeufe um die
    # Worker. Wird ``WORKERS`` gesenkt, gehoert diese Schwelle mitgesenkt.
    #
    # **Sie faellt nicht mehr mit ``max_text_chars`` zusammen.** Das war
    # frueher der Fall und als "eine einzige Zahl im ganzen Werkzeug"
    # begruendet. Der Zusammenfall war aber ein Nebeneffekt, kein Zweck: Die
    # beiden Zahlen beantworten verschiedene Fragen -- diese hier, wieviel
    # Text der Balancer zeitlich durchlaesst, jene, ab wann spaCy an
    # ``nlp.max_length`` bricht. Sie muessen nicht gleich sein und sind es
    # seit dieser Messung nicht mehr.
    #
    # Der Adapterlauf, den diese Pruefung kostet, ist gemessen billig: 0.0
    # bis 4.9 % der Gesamtdauer, mit fallendem Anteil bei groesseren
    # Dokumenten. Genau bei den Dateien, bei denen die Entscheidung zaehlt,
    # ist das Oeffnen also fast gratis.
    sync_threshold_chars: int = 400_000
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
