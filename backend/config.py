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
    # Am 2026-08-28 in Produktion ueber ALLE vier Formate gemessen:
    # **100'000**.
    #
    # **Die Zahl haengt an der SEGMENTZAHL, nicht an der Zeichenzahl.** Je
    # Segment faellt ein spaCy-Lauf mit eigenem Overhead an; ein ``.txt`` ist
    # EIN Segment, dieselben 100'000 Zeichen sind als docx/xlsx/pptx 427
    # Segmente à 234 Zeichen (ein Absatz bzw. eine Zeile je Segment).
    #
    # Gemessen auf dem Knoten mit drei Workern:
    #
    #   txt    395'000 Zeichen -> ~123 s   (~3'200 Zeichen/s)
    #   docx   149'000 Zeichen -> ~272 s   (  ~545 Zeichen/s)  zweimal
    #   xlsx   100'000 Zeichen -> ~181 s   (  ~553 Zeichen/s)
    #   pptx   100'000 Zeichen -> ~184 s   (  ~543 Zeichen/s)
    #   xlsx   200'000 Zeichen -> 504 nach 300 s -- Grenze gerissen
    #
    # **Die drei Office-Formate sind praktisch identisch** (543-553
    # Zeichen/s); nur ``.txt`` ist mit Faktor sechs der Ausreisser nach
    # oben. Massgeblich ist deshalb der Office-Wert -- und der gilt fuer das,
    # was im Betrieb tatsaechlich hochgeladen wird.
    #
    # **Zwei Fassungen dieser Konstante waren vorher falsch**, beide aus
    # demselben Grund: aus dem guenstigsten Fall abgeleitet.
    #   * 400'000 -- aus txt gerechnet. xlsx riss damit die Grenze.
    #   * 150'000 -- aus xlsx gerechnet, aber ohne docx zu messen. Ergab bei
    #     149'000 Zeichen 273 s von 300 s: nur 24 s Reserve, und der Upload
    #     zaehlt mit (die Balancer-Uhr laeuft ab Verbindungsaufbau).
    #
    # **Seit dem 2026-08-28 ist diese Zahl nur noch eine NOTBREMSE**, nicht
    # mehr die scharfe Schranke. Die Entscheidung faellt jetzt ueber die
    # geschaetzte Dauer (``sync_budget_sekunden`` unten) -- die Zeichenzahl
    # allein konnte nie beide Faelle richtig bedienen: Am langsamsten Format
    # ausgerichtet bremste sie ``.txt`` um Faktor sechs aus, am schnellsten
    # ausgerichtet riss sie bei Office-Dateien die Balancer-Grenze. Genau
    # dieser Zielkonflikt hat an einem Tag zwei falsche Fassungen erzeugt
    # (400'000 aus txt, dann 150'000 aus xlsx ohne docx zu messen).
    #
    # Was sie weiterhin leistet: Sie faengt den Fall ab, in dem die
    # Schaetzung selbst danebenliegt -- ein Dokument, dessen Segmente ganz
    # anders geschnitten sind als alles Gemessene. Ohne sie haenge die
    # einzige Schranke an einer Formel. 800'000 liegt oberhalb dessen, was
    # die Schaetzung je durchlassen wuerde (ein Einzelsegment dieser Groesse
    # schaetzt auf 222 s und faellt schon am Budget), und unterhalb von
    # ``nlp.max_length`` (1'000'000), an dem spaCy roh bricht.
    #
    # **Lokal ist davon NICHTS messbar** -- dort liegt xlsx bei 0.037 s je
    # 1000 Zeichen gegen 0.056 fuer txt, also genau andersherum. Der
    # Unterschied entsteht erst auf dem schwaecheren Knoten. Wer diese
    # Schwelle je neu ableitet: **auf dem Knoten, und mit einem
    # Office-Format.**
    #
    # Sie haengt zusaetzlich an ``WORKERS`` in ``deploy/pythonenv`` (heute 3):
    # Mit zwei Workern streuten dieselben 270'000 Zeichen zwischen 63 s und
    # 127 s, mit dreien nur noch zwischen 61 s und 64 s. Wird ``WORKERS``
    # gesenkt, gehoert diese Schwelle mitgesenkt.
    #
    # **Sie faellt nicht mehr mit ``max_text_chars`` zusammen.** Das war
    # frueher der Fall und als "eine einzige Zahl im ganzen Werkzeug"
    # begruendet. Der Zusammenfall war aber ein Nebeneffekt, kein Zweck: Die
    # beiden Zahlen beantworten verschiedene Fragen -- diese hier, wieviel
    # Text der Balancer zeitlich durchlaesst, jene, ab wann spaCy an
    # ``nlp.max_length`` bricht.
    #
    # Der Adapterlauf, den diese Pruefung kostet, ist gemessen billig: 0.0
    # bis 4.9 % der Gesamtdauer, mit fallendem Anteil bei groesseren
    # Dokumenten. Genau bei den Dateien, bei denen die Entscheidung zaehlt,
    # ist das Oeffnen also fast gratis.
    sync_threshold_chars: int = 800_000

    # ── Zeitschaetzung statt Zeichenzahl ────────────────────────────────
    #
    # Die Zeichenschwelle oben ist ein Stellvertreter fuer das, worum es
    # wirklich geht: **wie lange die Verarbeitung dauern wird.** Sie muss
    # deshalb am langsamsten Format ausgerichtet sein und bremst dabei
    # ``.txt`` unnoetig aus -- eine Textdatei mit 300'000 Zeichen ist in
    # rund 90 s fertig, laeuft heute aber als Hintergrundauftrag.
    #
    # Die beiden Werte hier schaetzen die Dauer direkt. Gemessen am
    # 2026-08-28 in Produktion (3 Worker), Ausgleichsrechnung ueber neun
    # Punkte in vier Formaten:
    #
    #     Dauer ~= 0.3625 s je Segment + 0.277 s je 1000 Zeichen
    #
    # Die Office-Formate trifft das auf ±2 % (docx/xlsx/pptx, 214 bis 637
    # Segmente). Der Segmentanteil dominiert dort deutlich -- je Segment
    # laeuft ein eigener spaCy-Durchgang mit festem Overhead, und genau der
    # macht 100'000 Zeichen als docx sechsmal teurer als dieselben Zeichen
    # als txt.
    #
    # **Bei ``.txt`` schaetzt das Modell zu hoch**, und das ist bewusst so
    # belassen: Ein einzelnes Segment von 100'000 Zeichen dauert gemessen
    # 11 s, das Modell sagt 28 s. Die Abweichung geht in die sichere
    # Richtung (frueher Auftrag statt spaeter 504), und ein Sonderfall fuer
    # sehr grosse Einzelsegmente waere eine zweite Kurve, die niemand
    # nachmisst. Trotz Ueberschaetzung darf ``.txt`` damit rund 700'000
    # Zeichen synchron -- siebenmal mehr als heute.
    sekunden_je_segment: float = 0.3625
    sekunden_je_1000_zeichen: float = 0.277

    # Wieviele Sekunden ein synchroner Lauf hoechstens dauern darf.
    #
    # Der Balancer schneidet nach **300 s** ab (gemessen, siehe
    # ``sync_threshold_chars``). 200 s laesst ein Drittel Reserve fuer das,
    # was diese Schaetzung NICHT sieht: den Upload (die Balancer-Uhr laeuft
    # ab Verbindungsaufbau), einen zweiten grossen Lauf auf demselben
    # Worker, und ein Dokument, das dichter ist als die Messreihe.
    #
    # Die Asymmetrie traegt diese Zahl: Zu hoch heisst ein ``504`` mitten
    # in der Verarbeitung -- zu tief heisst ein Hintergrundauftrag, der
    # zuverlaessig funktioniert.
    sync_budget_sekunden: float = 200.0
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
