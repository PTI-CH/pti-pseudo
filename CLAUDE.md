# CLAUDE.md

## Assistent-Identität

In diesem Repository identifiziert sich der Assistent als **PTI-Pseudonymisierer**.
Bei Begrüssungen, Selbstvorstellungen und Signaturen wird dieser Name verwendet.

## Was dieses Repository ist

Eine **Kopie der Presidio-Codebasis** (`data-privacy-stack/presidio`, MIT, ehemals
`microsoft/presidio`) mit vollständiger Historie, ergänzt um die PTI-Anwendung.
Presidio ist hier bewusst **nicht** ein PyPI-Paket, sondern liegt im Baum und wird
per Editable-Install geladen:

```bash
pip install -e ./presidio-analyzer -e ./presidio-anonymizer
```

Damit wirkt jede Änderung an `presidio-analyzer/presidio_analyzer/…` sofort — das
war der ganze Zweck des Kopierens statt eines `pip install presidio-analyzer`.

Das Fachkonzept liegt im Schwester-Repo `landing` unter
`frontend/public/ideen/pseudonymizer.html` (§1–§15) und ist live unter
<https://pti-tip-hub.ch/ideen/pseudonymizer.html>. Umgesetzt sind daraus (§12)
die Phasen **1, 2, 3 und 5**: Engine + GUI, Kategorienwahl und externe API,
die KI-Schicht fuer Personennamen, xlsx und pptx. Offen sind PDF/OCR (Phase 6)
und der SaaS-Vollausbau mit Async-Jobs (Phase 4).

## Verzeichnis-Konvention (wichtig für Upstream-Merges)

`upstream` ist als zweites Remote konfiguriert (Push absichtlich deaktiviert).
Auffrischen: `git fetch upstream && git merge upstream/main`.

**Regel: kein PTI-Inhalt in einer Datei, die upstream auch besitzt.** Alles Eigene
lebt in Pfaden, die es upstream nicht gibt:

| PTI | Upstream — nicht anfassen |
|---|---|
| `backend/` `frontend/` `deploy/` `scripts/` `golden-set/` | `presidio-*/` `docs/` (ausser `docs/superpowers/`) `e2e-tests/` |
| `.github/workflows/deploy-prod.yml` | die 6 Upstream-Workflows |
| `pti-requirements.txt` | `pyproject.toml` `.gitignore` `.env` `docker-compose.yml` |
| `CLAUDE.md` `JELASTIC_SETUP.md` `.env.example` | `CHANGELOG.md` `LICENSE` `NOTICE` |

### Zwei Fallen, die bereits zugeschnappt sind

1. **Presidios Root-`.gitignore` enthält `lib/`.** Ein `backend/lib/` wäre
   unbemerkt ignoriert worden — deshalb heisst das Verzeichnis `backend/core/`.
   Vor dem Anlegen neuer Ordner: `git check-ignore -v <pfad>`.
   Ebenfalls betroffen wären `build/`, `dist/`, `var/`, `parts/`, `wheels/`.
   Node-Artefakte deckt sie **nicht** ab → eigene `frontend/.gitignore`.
2. **Upstream bringt 6 eigene Workflows mit**, davon 3 mit `push: [main]`.
   Sie sind serverseitig deaktiviert (`gh workflow disable`), **nicht gelöscht** —
   Löschen erzeugt bei jedem Upstream-Merge einen Konflikt.
3. **`README.MD` ist die eine bewusste Ausnahme von der Regel oben.** Upstream
   schreibt den Namen gross, wir bräuchten `README.md` — auf Windows ist das
   dieselbe Datei. Ein Paar, das sich nur in der Gross-/Kleinschreibung
   unterscheidet, wäre bei jedem Checkout auf dieser Plattform eine Falle,
   und die Startseite des Repositories soll ohnehin das PTI-Werkzeug
   beschreiben. Die Datei trägt deshalb unseren Inhalt unter dem
   Upstream-Namen. **Bei einem Upstream-Merge konfliktiert sie — die Auflösung
   ist immer `--ours`:**

   ```bash
   git checkout --ours README.MD && git add README.MD
   ```

## Qualitaet der Erkennung messen

Die Testsuite prueft, **ob** der Code laeuft. Das Golden-Set prueft, **wie gut**
er erkennt — ohne es kann eine Verbesserung an einem Erkenner einen anderen
still verschlechtern.

```bash
.venv/Scripts/python -m pytest backend/tests/test_golden_set.py -v   # Gate
.venv/Scripts/python scripts/golden_set_report.py                    # Bericht
```

Der Bericht listet auf, **was** verfehlt wurde und **was** ein Fehlalarm war —
mit Dokumentname und Ampelstufe. Das ist der Einstieg fuer jede Verbesserung an
der Engine.

### Ein Fund gilt im ganzen Dokument

Das NER-Modell entscheidet **je Vorkommen**: derselbe Name wird im
ausformulierten Satz erkannt und in der Stichwortzeile daneben übersehen.
Ersetzt würde ohne Gegenmassnahme nur die erkannte Stelle — im Ergebnisdokument
stünde der Klarname weiter da, obwohl er in der Befundliste steht. Genau dieses
Fehlerbild kam aus dem Betrieb.

`pipeline.propagate_findings` trägt deshalb jeden Fundtext an allen weiteren
Stellen nach. Drei Grenzen, die dabei gelten und die man kennen muss, bevor man
daran schraubt:

1. **Nur `PERSON`, `LOCATION`, `ORGANIZATION`** (`entities.NER_ENTITIES`).
   Muster- und Begriffslisten-Erkenner finden ihren Text ohnehin überall.
2. **Erweitern und ergänzen, nie umbenennen.** Ein Nachtrag auf einer bereits
   erkannten Stelle wird verworfen — sonst überschriebe der häufigere Typ
   korrekt erkannte Funde des anderen.
3. **Wortgrenzengenau und gross-/kleinschreibungsempfindlich**, mindestens drei
   Zeichen. Ein Fehlalarm auf einem kurzen Kürzel vervielfachte sich sonst über
   das ganze Dokument.

Der Wächter darüber ist das Golden-Set-Dokument `wiederholte_nennung`; ohne den
Schritt verfehlt es 5 von 6 Vorkommen.

Drei Regeln:

1. **Marken nicht nachziehen.** Reisst eine Zielmarke, ist die Erkennung
   schlechter geworden — nicht die Marke zu hoch. Zielmarken und Messstand
   stehen in `golden-set/README.md`.

   Das Feld `nur_messen` am `Testdokument` ist **kein Schlupfloch dafür**. Es
   ist für Dokumente gedacht, die eine *Reichweitengrenze* des Werkzeugs
   vermessen statt eine Zusicherung zu prüfen — heute nur
   `franzoesische_namen`, weil das Modell deutsch trainiert ist. Solche
   Dokumente stehen im Bericht in einem eigenen Block und sperren nicht.
   Wird eine Marke gerissen, **weil die Erkennung schlechter geworden ist**,
   gehört das behoben und nicht umdeklariert.
2. **Echte Umlaute in Testdaten.** Das Modell haelt `Spaeter` faelschlich fuer
   einen Personennamen, `Später` nicht. Ein Golden-Set in ae/oe/ue-Schreibweise
   misst die falsche Groesse.
3. **Keine echten Dokumente.** `golden-set/` ist per `.gitignore` auf
   `synthetic/` beschraenkt. Reale PTI-Dokumente enthalten genau die
   Personendaten, die dieses Werkzeug entfernen soll.

### Checkliste nach jedem `git merge upstream/main`

```bash
gh workflow list --repo pti-schweiz/pti-pseudo --all   # neue Upstream-Workflows deaktivieren
git check-ignore -v backend/core deploy frontend/src             # muss leer bleiben
.venv/Scripts/python -m pytest backend/tests -q                  # Golden-Set-Gate
```

## Hausregeln

- **UI-Texte, Code-Kommentare und Commit-Messages auf Deutsch.**
  Conventional Commits mit Scope: `feat(pseudonymisierer): AHV-Recognizer mit EAN-13-Prüfziffer`.
- **Bezeichner englisch:** `pseudonymizer` — Kachel-ID, `AppKey`, DB-Spalte
  `pseudonymizer_access`. Die Subdomain weicht ab: `pseudo.pti-tip-hub.ch`
  (siehe `LIVE_URL` in `.github/workflows/deploy-prod.yml`). Nur das Repo
  heisst deutsch.
- **Vor jedem Push auf `main` und jedem Deploy beim Nutzer nachfragen.**
  `main` löst über die GitHub-Action einen Produktiv-Deploy aus.
- Umsetzung von Plänen **subagent-getrieben** (`superpowers:subagent-driven-development`).
- **Keine echten Personendaten ins Repo** — auch nicht in Tests, Fixtures oder
  Issues. `golden-set/` ist per `.gitignore` auf `synthetic/` beschränkt.

## Architektur in einem Absatz

FastAPI (`backend/main.py`) serviert die REST-API unter `/api/v1` **und** das
statisch gebaute React-SPA aus `frontend/dist` same-origin — kein CORS. Davor
hängen zwei Middlewares: HTTPS-Redirect (Jelastic reicht Port 80 unverschlüsselt
durch) und das TIP-Hub-SSO-Gate. Dieselben Router hängen ein zweites Mal unter
`/api/ext/v1` — der Maschinenpfad für andere PTI-Anwendungen, geschützt durch
in der Datenbank gehashte API-Schlüssel (Verwaltung: `/api/v1/api-keys`, nur
SSO, nur Admins) statt durch das SSO-Gate. Die Engine (`backend/engine/`) baut
auf der Presidio-`AnalyzerEngine` auf, mit deutschem spaCy-Modell und eigenen
Schweizer Erkennern. Der Vault (`backend/vault/`) hält die Zuordnung
Original→Pseudonym AES-GCM-verschlüsselt in PostgreSQL, damit
`documents:reverse` funktioniert.

## TIP-Hub SSO (Ansatz B)

Das Tool hält **keine User-DB und kein Token-Secret**. Es liest das geteilte
`refresh_token`-Cookie (Domain `.pti-tip-hub.ch`, gesetzt vom CRM) und fragt
server-zu-server `GET {CRM_BASE_URL}/api/auth/session`. Geprüft wird
`apps.pseudonymizer === true`. Vorbild: `pti-translator/backend/lib/hubAccessAuth.js`.

Nicht offensichtlich:
- **Fail closed.** CRM nicht erreichbar → nicht authentisiert. `httpx`-Timeout
  explizit setzen, sonst wird aus „fail closed" ein „fail hang".
- `user.person` **kann `None` sein**; es gibt kein Top-Level-`email`/`name`.
  Stabiler Schlüssel ist `username`, nicht die UUID.
- `apps.<tool>` und `user.role` sind **orthogonal** — ein Admin hat nicht
  automatisch Tool-Zugriff.
- Admin-Rechte kommen **ausschliesslich** aus `role === "admin"`. Bewusst keine
  Env-Allowlist, die auseinanderlaufen könnte.
- Der Verdict-Cache liegt **pro gunicorn-Worker**. Bei 2 Workern also bis zu
  2 Introspections pro Seitenload — akzeptiert.
- `X-Relay-Secret` ist ein Rate-Limit-Bypass beim CRM, **kein Auth-Faktor**.
- `/api/health` ist von SSO **und** HTTPS-Redirect ausgenommen (Smoke-Test).

Lokal ohne CRM: `HUB_AUTH=off` (synthetischer Admin; `DEV_ROLE=standard` prüft
den Verweigerungspfad).

## Externe API

`/api/ext/` ist vom SSO-Gate ausgenommen — **nicht** vom HTTPS-Redirect — und
darf nur mit `dependencies=[Depends(require_api_key)]` eingehängt werden
(siehe `DUAL_MOUNT_ROUTERS` in `main.py`). `api_keys.router` gehört **nie** in
diese Liste: sonst könnte ein Schlüssel sich selbst weitere Schlüssel
ausstellen. `HUB_AUTH=off` öffnet den ext-Pfad nicht — er verlangt auch lokal
einen gültigen Schlüssel. Der Aktor eines Maschinen-Aufrufs ist
`apikey:<Schlüssel-ID>` — **nie die Bezeichnung**, die ist nicht eindeutig; die
Auswertung `GET /api/v1/api-keys/usage` (nur SSO, nur Admin) gruppiert danach.
Die 140-Zeichen-Grenze für `label` ist seither nur noch historisch begründet.
**`DELETE /mappings/{id}` ist für Schlüssel gesperrt**
(403) — die einzige Ausnahme von der Vollberechtigung: Löschen ist
unumkehrbar und bleibt Administratoren in der Oberfläche vorbehalten.
Details: [API.md](API.md).

## KI-Schicht für Personennamen (Phase 3, Variante C)

`backend/ki/` prüft über Infomaniak AI die **Grenzen** gefundener
Personennamen. Sie behebt den Namenssplit: `Sandra Zbinden` wird von spaCy nur
als `Sandra` erkannt, der Nachname bleibt stehen — und das Dokument sieht
trotzdem pseudonymisiert aus. Dagegen hilft weder Muster noch Prüfziffer noch
die Wortart-Regel, nur ein Modell, das den Satz liest.

Vier Entscheidungen, die man kennen muss, bevor man daran schraubt. Die
Begründungen samt Ausbaustufen stehen in
`docs/superpowers/specs/2026-08-24-ki-schicht-namen-design.md`.

1. **Die Namensprüfung schickt nur Fenster hinaus** — rund 20 Wörter um jeden
   Fundkandidaten. Sie läuft **vor** `propagate_findings`, damit der
   korrigierte vollständige Name anschliessend durchs Dokument getragen wird;
   die Reihenfolge in `detect_in_segments` ist nicht beliebig.
2. **Bei Ausfall weiterlaufen**, nicht scheitern. Die lokale Erkennung ist mit
   95 % Namen-Recall der Normalbetrieb.
3. **Nie entfernen, und nur gefahrlos verkleinern.** Ein zu Unrecht
   verworfener Fund kostet einen Klarnamen, und zwar unsichtbar. Verkleinern
   ist erlaubt, solange **kein grossgeschriebenes Wort wegfällt** — nur ein
   solches könnte ein Namensteil sein, der danach im Klartext stünde.
4. **Vorgabe aus auf API-Ebene**, vorangehakt in der Oberfläche. Sonst begänne
   `/api/ext/v1` ab einem Deploy stillschweigend, Inhalte hinauszuschicken.

### Stufe D: die Zweitmeinung sieht das ganze Dokument

`backend/ki/zweitmeinung.py` maskiert **alle** erkannten Funde und fragt zum
Ergebnis: „Stehen hier noch Personendaten?" Was gemeldet wird, hat die lokale
Erkennung übersehen — und lässt sich leicht zurückschreiben, weil ein
übersehener Name im Original wortgleich dasteht.

**Das ist die Stelle, an der sich die Datenschutzaussage des Werkzeugs
ändert.** Der vollständige Text jedes Dokuments geht hinaus, ohne die erkannten
Namen, aber mit dem gesamten Sachverhalt. Auch ein Dokument, in dem lokal
**nichts** gefunden wurde, geht hinaus — gewollt, denn genau dort ist eine
Zweitmeinung etwas wert. Gemessen (2026-08-24): Stufe A überträgt 66.5 % des
Golden-Set-Textes und in dichten Passagen bis 371 %, weil Fenster überlappen;
Stufe D 100 %, davon keinen erkannten Namen.

Vier Dinge, die dabei zusammenhängen:

- **Sie ist ab Werk AUS, und das ist gemessen.** Am 2026-08-25 am Golden-Set
  gegen den echten Dienst: ohne sie 93.2 % Precision / 100 % Recall / PERSON
  100 %, mit ihr 87.2 % / 98.6 % / 84 %. **Sie findet nicht das Falsche — sie
  zerstört das Richtige:** Sie verdreifacht die Anfragen je Dokument, der
  Dienst antwortet unter dieser Last mit Müll statt mit einem sauberen 429,
  und die *Namensprüfung* fällt aus (im Messlauf 26-mal). Zuschalten lohnt
  erst, wenn die Anfragezahl gesenkt ist — überlappende Fenster
  zusammenfassen ist der grösste Hebel — oder Infomaniak das Kontingent
  anhebt.
- **`KI_ZWEITMEINUNG=on`** schaltet sie im Dashboard zu, ohne Deploy. Die
  Variable muss in der Export-Liste von `deploy/pythonenv` stehen, sonst
  erreicht sie den Dienstprozess nie. Sie ist die *Betriebs*-Entscheidung;
  `ki_pruefung` je Lauf bleibt die des Nutzers, beide müssen zutreffen.
- **Rechtlich ist die Stufe getragen.** Der Auftraggeber hat am 2026-08-25
  bestätigt, dass der jCloud-Auftragsverarbeitungsvertrag die AI Tools mit
  umfasst. Der Schalter ist deshalb **kein Notaus**, sondern eine reine
  Betriebsentscheidung; sie steht aus einem *gemessenen* Grund auf aus.
- **Neue Funde von hier sind `gelb`, nicht grün** (Score 0.7): Keine lokale
  Ebene hat sie bestätigt.
- **`propagate_findings` läuft danach ein zweites Mal**, sonst gälte ein neu
  gefundener Name nur an der gemeldeten Stelle.

Fünf am echten Dienst gemessene Eigenheiten stecken in
`backend/ki/infomaniak.py`: `response_format: json_object` ergibt **HTTP 400**
(nur `json_schema` geht), **HTTP 200 mit leerem `content` ist ein Fehler** (vier
Modelle antworten so bei knappem Token-Budget), und die **Produkt-Id steht im
Pfad**, nicht im Kopf. Dazu zwei, die am 2026-08-25 dazukamen:

- **`json_schema` erzwingt die Antwortform nicht, es lenkt nur — und das
  Verhalten ändert sich ohne Vorwarnung.** Am 2026-08-25 lieferte die
  Namensprüfung vormittags 9/9 und nachmittags 0/3, ohne dass sich an unserem
  Code oder am Modellnamen etwas geändert hätte: Das Modell antwortete plötzlich
  in Prosa („Ist die Markierung Teil eines Personennamens? Ja …") statt in JSON.
  Eine ausdrückliche Formatangabe **mit Beispiel** in der Anweisung stellte
  3/3 wieder her.

  **Regel daraus: Jede Anweisung an den Dienst nennt das Antwortformat und
  zeigt ein Beispiel.** Wer sich auf das Schema allein verlässt, baut auf einen
  Zufall, der eines Tages anders ausfällt — und der Ausfall ist still, weil die
  Schicht sauber auf die lokale Erkennung zurückfällt. Bei der Zweitmeinung
  dasselbe Bild von der anderen Seite: ohne Beispiel neunmal von neun eine
  leere Liste, gültig, schnell und wertlos.
- **Nachrangig: der Dienst drosselt mit HTTP 429.** Nicht dokumentiert, aber
  erreichbar — beobachtet nach mehreren Stunden Messläufen im Sekundentakt, also
  weit jenseits des normalen Gebrauchs. Wo die Grenze genau liegt, ist **nicht**
  ausgemessen; für den Alltag mit einer Handvoll Nutzern ist bisher nichts
  aufgefallen. Der Wiederholungsversuch hilft dagegen nicht, weil er ohne Pause
  erfolgt — er verdoppelt die Last. Erst wenn die Erkennung im Betrieb auffällig
  oft auf das lokale Ergebnis zurückfällt, lohnt der Blick: Im Protokoll steht
  429 im Klartext, und dort ist er von einem echten Ausfall unterscheidbar — in
  der Oberfläche nicht.

Das Modell ist `Ministral-3`, nicht Apertus: Massenklassifikation, nicht Chat.

### Tesseract auf dem Knoten

OCR (Phase 6, Stufe 2) braucht das **Systembinary** `tesseract`. Der Benutzer
`python` hat kein `sudo`, `yum install` ist also ausgeschlossen. Seit dem
2026-08-28 liegen die RPMs aus AlmaLinux-AppStream entpackt unter
`/home/jelastic/opt/tesseract` (tesseract 4.1.1, langpack-deu/-eng,
leptonica, giflib) — **ausserhalb von `ROOT`**, damit ein VCS-Update sie
nicht wegräumt, aus demselben Grund wie `pythonenv` selbst.

**Der Punkt, an dem es beinahe still gescheitert wäre:** Der Dienst-`PATH`
enthält nur die Systempfade. `backend/ocr/verfuegbarkeit.py` sucht per
`shutil.which` — ohne die drei Exporte in `deploy/pythonenv` (`PATH`,
`LD_LIBRARY_PATH`, `TESSDATA_PREFIX`) findet es nichts, `OCR_ENABLED=true`
bliebe **wirkungslos**, und jeder Scan würde weiter abgewiesen, ohne dass
irgendwo steht warum. Die Prüfung ist absichtlich zweistufig (`which`, dann
`get_tesseract_version()`), fällt aber genauso still aus.

Vier Dinge, die daran hängen:

1. **Ein Knotenumzug bringt Tesseract nicht mit.** Die Dateien liegen im
   Home, nicht im Repository — eine neue Umgebung hat sie nicht. Das
   Verfahren: RPMs mit `yum download` holen (`--disablerepo='*'
   --enablerepo='appstream,baseos,crb,epel,extras'`, sonst bricht ein
   kaputtes PostgreSQL-Repo den Lauf ab) und ohne Root entpacken.
2. **`cpio` fehlt auf dem Knoten.** `rpm2cpio` allein nützt deshalb nichts;
   entpackt wurde mit `rpmfile` + `zstandard` (die RPMs sind zstd-komprimiert)
   über `pip install --user`.
3. **RPM-Symlinks kommen als Textdateien an.** `libtesseract.so.4` enthält
   dann den Namen des Ziels statt darauf zu zeigen, und das Binary startet
   mit „file too short". Sie müssen nachträglich zu echten Symlinks gemacht
   werden — **anhand des Inhalts, nicht der Dateigrösse**: Ein Test auf
   „kleiner als N Bytes" trifft auch echte Bibliotheken und zerstört sie.
4. **Tesseract 4.1.1, nicht 5.x.** Mehr gibt AppStream für AlmaLinux 9 nicht
   her; EPEL 9 führt weder `tesseract` noch `leptonica` (nachgesehen).

Nachgemessen nach dem Deploy: `ocr_verfuegbar()` meldet in Produktion `True`,
ein deutscher Testsatz wurde zeichengenau erkannt.

### Was von einem Fundtext ins Knotenprotokoll darf

Die Messreihe zu Ort und Organisation (`pipeline._ki_messen_ort_organisation`)
fragt die KI zu jedem `LOCATION`- und `ORGANIZATION`-Fund und **ändert
nichts**. Ihr Ergebnis steht im Knotenprotokoll — und damit an einem Ort, der
**nicht** unter der Vault-Verschlüsselung liegt und den logrotate tagelang
aufhebt.

Ein `LOCATION`-Fund kann eine Wohnadresse sein. Deshalb geht der Fundtext
durch `pipeline.protokolltext`: durchgelassen wird nur, was **kurz** (≤ 30
Zeichen) **und einteilig** (kein Leerzeichen) ist; alles andere wird durch
`<mehrteilig:N>` bzw. `<lang:N>` ersetzt.

Drei Dinge, die dabei zusammenhängen:

1. **Der Filter nimmt der Messung nichts.** Sie existiert für genau eine
   Frage: Trennt die KI Gattungswörter (`Fallnummer`, `Kontrollschild`,
   `Intern`) von echten Eigennamen? Diese Klasse ist immer ein einzelnes
   kurzes Wort — sie passiert den Filter vollständig. Ein mehrteiliger Fund
   war für die Frage ohnehin wertlos.
2. **Beide Bedingungen, nicht eine.** Kurz allein genügt nicht: `Bern,
   Bahnhof` ist kurz und schon mehrteilig, und gerade die Kombination aus
   Ort und Detail macht eine Angabe identifizierend.
3. **Gefiltertes verschwindet nicht spurlos.** Typ und Urteil der KI werden
   weiter protokolliert, nur der Text wird durch seine Länge ersetzt. Ohne
   diese Spur sähe eine Auswertung nach lauter kurzen Gattungswörtern aus,
   und niemand wüsste, dass die mehrteiligen Funde fehlen — die Messung wäre
   still verzerrt.

Der Wächter ist `test_messung_protokolliert_keine_adresse`; er lässt bewusst
die **echte** Messung laufen statt nur `protokolltext` zu prüfen — ein Test
auf den Helfer allein bliebe grün, während die Aufrufstelle den Fundtext
weiter ungefiltert hineinreicht (derselbe Fehler wie 2026-08-25 beim
Protokoll).

## Schemaänderungen

`init_db()` im Lifespan ruft `create_all` — das legt **fehlende Tabellen** an,
aber keine fehlenden **Spalten**. Eine neue Spalte in einem bestehenden Modell
entsteht in Produktion also nicht von selbst, und sie fällt nicht beim Deploy
auf, sondern beim ersten Zugriff: `column … does not exist`. Weil die
Begriffsliste bei *jedem* Lauf gelesen wird, wäre das Werkzeug damit ganz
ausser Betrieb.

Deshalb: **jede neue Spalte gehört in `NACHZUZIEHENDE_SPALTEN` in
`backend/db.py`.** `ensure_schema()` zieht sie im Lifespan nach — idempotent,
rein additiv (kein `DROP`, kein `RENAME`), serialisiert über einen
PostgreSQL-Advisory-Lock gegen den Wettlauf zweier gunicorn-Worker.

**Nicht als Deploy-Schritt über SSH.** Am 2026-08-19 versucht und gemessen: Die
Dashboard-Variablen des Knotens gelten für den Dienstprozess, **eine
SSH-Sitzung erbt sie nicht** — das Skript brach mit `RuntimeError: DATABASE_URL
ist nicht gesetzt` ab und der Deploy blieb vor dem Neustart stehen. Derselbe
Stolperstein trifft jedes andere Skript, das über den SSH-Gate an die Datenbank
will.

`scripts/schema_nachziehen.py` bleibt als Hand-Werkzeug (`--pruefen` meldet nur)
für Umzüge und die Ein-Knoten-Variante; es benutzt dieselbe Liste.

### Alembic ist Buchhaltung, nicht Vollzug

Seit dem 2026-08-26 liegt `migrations/` im Baum, mit **einer** Revision:
`20260826_basis` beschreibt den Ist-Zustand. Sie ändert am Betrieb nichts —
`init_db()` und `ensure_schema()` laufen unverändert im Lifespan und sind
weiterhin das, was in Produktion tatsächlich wirkt. Die Anwendung ruft Alembic
an **keiner** Stelle auf; der Grund gegen einen Autorun beim Start (zwei
gunicorn-Worker, ein `upgrade head`, ein Wettlauf) gilt unverändert.

Vier Sätze, die zusammen die Regel ergeben:

1. **Die Basisrevision wird auf einer bestehenden Datenbank nie ausgeführt,
   sondern gestempelt.** `scripts/alembic_stempeln.py` prüft vorher (Tabellen
   da? Spalten da? schon gestempelt?), nennt die Zieldatenbank ohne Passwort
   und fasst ausschliesslich `alembic_version` an. `upgrade head` gilt nur für
   eine **leere** Datenbank; gegen eine bestehende bricht die Migration mit
   einer Erklärung ab.
2. **Eine neue Spalte braucht beides.** Den Eintrag in
   `NACHZUZIEHENDE_SPALTEN` — der wirkt — *und* eine Migration, die die
   Geschichte festhält; ohne sie reisst `backend/tests/test_alembic_basis.py`.
   Ein Widerspruch entsteht daraus nicht: In Produktion legt `ensure_schema`
   die Spalte an, danach wird die Datenbank **neu gestempelt**, denn sie *hat*
   den Stand ja bereits. Eilig ist das nie — zur Laufzeit liest niemand
   `alembic_version`.
3. **Kein `DROP`, kein `RENAME`, keine `server_default`-Änderung** ohne
   eigenes, bewusst geprüftes Vorhaben mit Backup davor. `downgrade` der
   Basisrevision **verweigert sich** und wirft: der Rückbau wäre ein
   `DROP TABLE mapping_entries` und damit der Verlust jeder Zuordnung.
4. **`--autogenerate` irrt, und zwar reproduzierbar.** An der Basisrevision am
   2026-08-26 beobachtet: `postgresql.JSONB(astext_type=Text())` ohne Präfix
   und ohne Import (die Datei war nicht einmal importierbar), und ein
   `with op.batch_alter_table(…)` um jedes `create_index`, weil der Lauf gegen
   SQLite ging. Jede erzeugte Datei wird gelesen, nicht nur committet.

Der Wächter dafür ist `backend/tests/test_alembic_basis.py`, und er vergleicht
bewusst **PostgreSQL-DDL, nicht SQLite**: Die beiden Typen, bei denen
Autogenerate irrt, sehen auf SQLite gleich aus (`JSON().with_variant(JSONB())`
rendert dort beidseitig `JSON`, `sa.Uuid` beidseitig `CHAR(32)`) und auf
PostgreSQL verschieden. Beide Seiten entstehen **ohne Datenbank** — die
Migration über Alembics Offline-Modus (`upgrade --sql`), die Modelle über
`create_mock_engine`.

Der Tag, an dem Alembic den Vollzug übernimmt, ist der Tag, an dem
`NACHZUZIEHENDE_SPALTEN` leergeräumt wird und `ensure_schema` verschwindet.
Das ist eine eigene Änderung mit eigenem Ausfallfenster — und sie setzt
voraus, dass `upgrade` in Produktion überhaupt laufen kann, also dass die
`DATABASE_URL`-Falle des SSH-Gates gelöst ist (siehe oben). Bis dahin ist der
Stempel ein Handgriff und **kein Deploy-Schritt**.

## Asynchrone Aufträge

Grosse Dateien laufen in den beiden Dateirouten (`anonymize-file`,
`reverse-file`) nicht mehr im offenen Request, sondern als Zeile in der
Tabelle `jobs` — sonst schneidet der Balancer eine grosse Verarbeitung ab,
bevor sie fertig ist. Details und Beispiele stehen in API.md; hier nur, was
man wissen muss, bevor man am Mechanismus schraubt.

**Zwei Schranken entscheiden das, und ihre Reihenfolge ist tragend.**
`documents._laeuft_als_auftrag` prüft erst `SYNC_THRESHOLD_BYTES` (2 MiB,
ohne die Datei zu öffnen), dann `SYNC_THRESHOLD_CHARS` (150'000, nach dem
Adapterlauf). Eine davon reicht zum Auftrag.

Der Grund für die zweite ist gemessen (2026-08-28,
`scripts/schwelle_messen.py`): **Die Dauer ist linear im Text** — rund
20'000 Zeichen je Sekunde über alle Formate, kein Knick, keine
Superlinearität —, und **die Dateigrösse sagt darüber fast nichts.**
Dieselben 64'107 Zeichen wiegen als txt 64 KiB, als docx 39 KiB und als xlsx
8 KiB; Faktor acht, und die Dichte steigt zusätzlich mit der Dokumentgrösse,
weil sich die Formatgrundlast amortisiert (ein docx trägt rund 36 KiB
Vorlage, Thema und Schriften ohne ein Zeichen Text). Mit der Byte-Schranke
allein war eine dichte Arbeitsmappe von 2 MiB über 17 Millionen Zeichen und
rund 15 Minuten unterwegs — weit jenseits dessen, was der Balancer offen
hält. Die Schwelle schützte nicht, wovor sie schützen sollte.

**Das Zeitbudget ist gemessen: der Balancer wartet 300 Sekunden.** Bis zum
2026-08-28 stand überall im Baum „eine Minute" — `proxy_read_timeout`s
NGINX-Vorgabe, keine Messung. Der echte Wert ist das **Fünffache**: Ein Lauf
über 280'000 Zeichen bekam nach exakt 300.5 s einen sauberen `504`, Läufe
von 63 bis 127 s gingen mehrfach durch. Gemessen über `/api/ext/v1` mit
einem auf sechs Stunden befristeten Schlüssel (danach widerrufen) und
synthetischen Dokumenten — ohne die Produktion umzukonfigurieren.

Drei Dinge, die man daraus mitnehmen muss:

1. **Der `504` ist eine Zeit-, keine Grössengrenze.** Derselbe Umfang lief
   beim zweiten Versuch in 67 s durch. Was streut, ist die
   Verarbeitungsdauer: dieselben 270'000 Zeichen brauchten 63 s, 124 s und
   127 s — Faktor zwei, allein durch die Konkurrenz um die zwei
   uvicorn-Worker.
2. **Der Durchsatz ist viel niedriger als gedacht** — rund 2'100 bis 4'300
   Zeichen/s statt der früher gerechneten 20'000/s. Die beiden Irrtümer
   haben sich teilweise aufgehoben: Budget fünfmal grösser, Durchsatz rund
   fünfmal kleiner. **`SYNC_THRESHOLD_CHARS` steht seit dem 2026-08-28 auf
   150'000**, gerechnet mit dem *langsamsten gemessenen Format*.

   **Die Schwelle hängt am Format, nicht nur an der Zeichenzahl.** Eine
   erste Fassung stand auf 400'000 und wurde am selben Tag wieder
   kassiert — die Nachmessung mit xlsx riss die Grenze:

   | Format | Zeichen | Dauer |
   |---|---|---|
   | txt | 395'000 | ~123 s |
   | xlsx | 100'000 | ~181 s |
   | xlsx | 200'000 | **`504` nach 300 s** |

   Faktor sechs bei gleicher Zeichenzahl. Der Grund steht in den Adaptern:
   Ein `.txt` ist **ein** Segment, dieselben 100'000 Zeichen als xlsx sind
   **427** Segmente à 234 Zeichen (eine Zeile je Segment), und je Segment
   fällt ein spaCy-Lauf mit eigenem Overhead an.

   **Lokal ist der Unterschied nicht sichtbar** — dort misst man 0.037
   (xlsx) gegen 0.056 (txt) s je 1000 Zeichen, also andersherum. Er
   entsteht erst auf dem schwächeren Knoten. Wer diese Schwelle neu
   ableitet, misst **auf dem Knoten und mit xlsx**.

   Die Zahl trägt bewusst keine dicke Reserve (150'000 × ~550 Zeichen/s
   ≈ 273 s von 300 s): Der Auftragspfad ist der sichere Ausweg, nicht der
   teure. Zu hoch heisst `504` mitten in der Verarbeitung, zu tief heisst
   ein Hintergrundauftrag, der zuverlässig läuft.

   **Sie hängt zusätzlich an `WORKERS`** (heute 3 statt 2): Mit zwei
   Workern streuten dieselben 270'000 Zeichen zwischen 63 s und 127 s, mit
   dreien nur noch zwischen 61 s und 64 s. Wird `WORKERS` gesenkt, gehört
   die Schwelle mitgesenkt.
   Sie fällt seither **nicht mehr** mit `max_text_chars` (300'000)
   zusammen; der Gleichstand war ein Nebeneffekt, kein Zweck — die beiden
   beantworten verschiedene Fragen.
3. **Gemessen wurde mit einem einzelnen Aufrufer.** Zwei gleichzeitige
   grosse Läufe teilen sich dieselben zwei Worker.

Zahlen und Auslegung: `docs/superpowers/specs/2026-08-25-asynchrone-auftraege-design.md` §12.

Der Grund für die Reihenfolge ist ein anderer: Die Byte-Prüfung ist die
einzige, die auch bei einer **beschädigten** Datei greift. `extract_segments`
wirft daran, und der Auftragspfad ist genau der Ort, an dem so ein Fehler
sauber als gescheiterter Auftrag endet statt als 500 mitten in der Anfrage.
Wer die Zeichenprüfung nach vorn zöge, tauschte eine ruhige Absage gegen
einen Serverfehler. Der Wächter dafür ist
`test_die_bytegrenze_greift_weiterhin_ohne_die_datei_zu_oeffnen`.

Zwei Zahlen dazu, die nicht offensichtlich sind. **300'000 und nicht die
gemessenen 360'000**: Das lässt Luft und fällt mit `max_text_chars`
zusammen, sodass es im ganzen Werkzeug eine einzige Zahl dafür gibt, wie
viel Text synchron verarbeitet wird. Und **der Adapterlauf, den die zweite
Prüfung kostet, ist billig** — gemessen 0.0 bis 4.9 % der Gesamtdauer, mit
fallendem Anteil bei grösseren Dokumenten. Im synchronen Zweig läuft er
dadurch zweimal; ihn durch `anonymize_document` durchzureichen hätte deren
Signatur für wenige Prozent verbogen.

**Der Arbeiter lebt im Prozess, nicht im System.** Der Jelastic-Knoten startet
genau einen Befehl (`python -m uvicorn … --workers 2`); der Benutzer `python`
hat kein `sudo` — kein systemd-Dienst, kein zweiter überwachter Prozess ist
möglich. Je uvicorn-Worker läuft deshalb ein `threading.Thread`
(`backend/jobs/arbeiter.py`), der die Warteschlange im Zwei-Sekunden-Takt
abfragt.

**Die Zuteilung geschieht in der Datenbank, nicht im Prozess.** Zwei Worker
greifen nach derselben wartenden Zeile; wählte jeder für sich aus, verarbeitete
die Pipeline dasselbe Dokument doppelt und der Vault vergäbe zwei Pseudonyme
für denselben Namen. `warteschlange.hole_naechsten` sperrt deshalb die Zeile
mit `FOR UPDATE SKIP LOCKED` auf PostgreSQL; SQLite (nur im Testbetrieb) hat
dafür ein bedingtes `UPDATE … WHERE status = 'wartend'` mit Prüfung der
betroffenen Zeilenzahl.

**Der Wächter dieser Zuteilung prüft die kompilierte SQL, nicht einen
SQLite-Faden.** `test_zuteilung_sperrt_die_zeile_und_ueberspringt_gesperrte`
(`backend/tests/test_jobs_warteschlange.py`) kompiliert
`warteschlange._wartende_abfrage()` gegen `sqlalchemy.dialects.postgresql.dialect()`
und prüft `"FOR UPDATE"`/`"SKIP LOCKED"` als Teilstrings der erzeugten SQL —
ohne Datenbank, ohne Fäden. Das sieht für einen Nebenläufigkeitstest
ungewöhnlich aus, ist aber Absicht: Ein Fadentest auf SQLite bewachte nur den
Zweig, der in Produktion nie läuft (SQLite kennt `SKIP LOCKED` gar nicht),
und wäre für die Regression, die tatsächlich schadet — `skip_locked`
verschwindet aus dem PostgreSQL-Zweig, oder `with_for_update` ganz —, blind
geblieben. Ein solcher Fadentest war ausserdem selbst flatterhaft, siehe
nächster Punkt.

**Der Arbeiter startet nur auf PostgreSQL — das ist keine Vorsicht, sondern
gemessen notwendig.** Die Testsuite läuft auf SQLite mit `StaticPool`: eine
einzige geteilte Verbindung für alle Sitzungen. Ein Hintergrundfaden, der
daneben alle zwei Sekunden pollt und committet, zerlegt gelegentlich deren
Transaktionszustand für alle anderen Sitzungen — gemessen am 2026-08-25:
`sqlite3.InterfaceError` in etwa 4 von 5 Läufen, sichtbar unter anderem als
ein direkt in der Datenbank angelegter API-Schlüssel, der für die nächste
Anfrage nicht mehr sichtbar war. Der Arbeiter prüft deshalb beim Start im
Lifespan `hintergrundverarbeitung_moeglich(get_engine().dialect.name)`; auf
jedem anderen Dialekt bleibt er aus und schreibt eine `logger.warning()` —
nicht, um einen liegen gebliebenen Auftrag zu erklären (grosse Dateien werden
seit dem nächsten Absatz gar nicht mehr angenommen), sondern damit, wer den
Knoten betreibt, im Protokoll sieht, WARUM Uploads oberhalb der Schwelle
scheitern, nicht nur, dass sie es tun. **Diese Schranke wirkt willkürlich,
ist es aber nicht: sie beim nächsten Aufräumen zu entfernen macht die
Testsuite wieder flatterhaft.**

**Ein Auftrag, den nie jemand abarbeiten würde, darf gar nicht erst
entstehen.** Dieselbe Schranke trifft auch die Ein-Knoten-Rückfallvariante
(`JELASTIC_SETUP.md`, Abschnitt 1b), nicht nur den Testbetrieb — dort läuft
`DATABASE_URL` ebenfalls auf SQLite, und der Arbeiter startet dort ebenso
wenig. Eine erste Fassung liess die Schwellenprüfung in `documents.py` den
Auftrag trotzdem anlegen: `202`, eine `job_id`, und der Auftrag blieb für
immer auf `wartend` stehen, während die Oberfläche endlos nachfragte, ohne
dass irgendwo stand, warum — dokumentiert zu haben reicht nicht, wenn
niemand die Dokumentation liest, während er auf einen Fortschrittsbalken
starrt. `warteschlange.lege_an` prüft deshalb selbst
`hintergrundverarbeitung_moeglich` und weist ab, statt eine Zeile
anzulegen; der Router übersetzt die Ablehnung in **`503`** mit einem Text,
der einen nächsten Schritt nennt (Datei aufteilen oder Administration
kontaktieren).

**Zwei Prüfungen in `lege_an`, und ihre Reihenfolge ist der Punkt.** Zuerst
das **Format** (`adapters.get_adapter`), erst danach die Verfügbarkeit: Was
an der *Anfrage* falsch ist, geht vor dem, was der *Installation* fehlt —
sonst läse, wer eine `.pdf` an einen Knoten ohne Arbeiter schickt, „bitte
teilen Sie die Datei auf", und die Hälfte einer `.pdf` ist keine `.pdf`
weniger. Vorher hing die Formatabsage an der **Dateigrösse**: Dieselbe `.pdf`
bekam unterhalb der Schwelle sofort `415` mit dem Hinweis „als DOCX
exportieren", oberhalb erst nach Anlegen, Zuteilen und Ausführen einen
gescheiterten Auftrag — und lag bis dahin verschlüsselt in der Datenbank.
Beide Dateirouten übersetzen `UnsupportedFormatError` jetzt in **`415`**,
unabhängig von der Grösse. Eine Falle daran: Der Rückfall für eine Datei
ohne Namen heisst in beiden Routen seither `unbenannt.txt` wie im synchronen
Zweig und nicht mehr `dokument` — ein Name ohne Endung fiele sonst in genau
diese Formatprüfung.

**Die Prüfungen sitzen bewusst in `lege_an`, nicht im Router davor.**
`lege_an` ist die einzige Stelle im ganzen Baum, an der überhaupt eine
Auftragszeile entsteht — eine Prüfung im Router (`documents.py`) hätte ein
künftiger zweiter Aufrufer (eine weitere Route, ein Skript) versehentlich
umgehen können, eine Prüfung in `lege_an` nicht. Die Ausnahme
(`AuftragNichtMoeglichError`) ist bewusst kein `HTTPException`: Das Modul
bleibt frei von HTTP (siehe Modul-Docstring in `warteschlange.py`); die
Übersetzung in eine Statuszeile geschieht im Router — dasselbe gilt für
`UnsupportedFormatError`, das aus `adapters` kommt und ebenfalls nichts von
HTTP weiss.

**Ein verwaister Auftrag kommt nach 10 Minuten zurück in die Warteschlange,
dreimal — danach gilt er als endgültig gescheitert.**
`warteschlange.gib_verwaiste_frei` setzt einen Auftrag, dessen `updated_at`
älter als `VERWAIST_NACH` (10 Minuten) ist, zurück auf `wartend`; ein
Auftrag, der so `MAX_FREIGABEN` (3) mal freigegeben wurde, geht stattdessen
auf `fehler`. Ohne diese Grenze würde ein Dokument, das den Arbeiter
zuverlässig zum Absturz bringt, für immer im Kreis wandern und bei jedem
Durchlauf einen Worker mitnehmen. Der Aufräumlauf (`raeume_ab`, entfernt
abgelaufene Aufträge samt Ergebnis) läuft im selben Faden, aber nur alle
fünf Minuten (`AUFRAEUMEN_ALLE`); abgelaufene Aufträge haben es nicht eilig.

**Der Aufräumtakt hängt an der Uhr und nicht an einer Durchgangszahl** —
seit dem 2026-08-28, vorher war es „jeder 150. Durchgang" mit „rund alle
fünf Minuten" daneben. Das stimmte nur im Leerlauf: Ein Durchgang wartet
`TAKT_SEKUNDEN` **nur, wenn er nichts gefunden hat**. Wer eine volle
Warteschlange abarbeitet, zählt 150 Durchgänge in der Zeit, die 150 Aufträge
brauchen — und räumte damit ausgerechnet unter Last am häufigsten auf, wo
die zusätzliche Abfrage am teuersten ist und am wenigsten bringt. Gemessen
wird auf der **monotonen** Uhr (eine Zeitumstellung darf den Takt nicht
aussetzen lassen), und sie wird **vor** dem Lauf gestellt: Wirft er, kommt
der nächste Versuch in fünf Minuten und nicht im Zwei-Sekunden-Takt. Der
Startwert ist „jetzt" und nicht „nie", damit der erste Durchgang nicht
aufräumt — der Lifespan hat das beim Prozessstart gerade getan.

Der Takt der Warteschlangen-Abfrage selbst bleibt dagegen bei zwei Sekunden,
auch im Leerlauf. Ihn zu strecken wäre die naheliegende zweite Sparmassnahme
und wäre falsch: Er ist die Zeit, die ein Mensch nach dem Hochladen einer
grossen Datei wartet, bevor überhaupt etwas beginnt. Eine Abfrage je Worker
und Sekunde gegen einen Index ist der günstigere Teil dieses Tauschs.

**Auch das Freigeben sperrt die Zeile** (`_verwaiste_abfrage`, dasselbe
`FOR UPDATE SKIP LOCKED` wie die Zuteilung). Sonst lesen zwei Worker im
selben Aufräumlauf dieselbe hängende Zeile und zählen beide eine Freigabe
darauf: Der Auftrag verbrauchte seine drei Versuche in der halben Zeit und
gälte als endgültig gescheitert, obwohl er nur zweimal wirklich unterbrochen
wurde. Der Rückgabewert von `gib_verwaiste_frei` fällt dadurch eher zu
niedrig aus — die richtige Richtung, denn die übersprungene Zeile gibt der
andere Worker ja gerade selbst frei.

**Die Verfallszeit gilt ab Anlage, und beim Prozessstart wird aufgeräumt.**
`melde_fertig`/`melde_fehler` schieben `expires_at` beim Ende ohnehin auf
24 h ab dann; `lege_an` setzt sie **zusätzlich schon bei der Anlage**, weil
`raeume_ab` ausschliesslich Zeilen **mit** `expires_at` findet. Gemeint ist
der Fall, in dem es nie zu einem Ende kommt: Scheitert der Arbeiterstart im
Lifespan — nicht am Dialekt, den fängt die 503-Schranke oben, sondern an
einem Importfehler oder einer beim Start nicht erreichbaren Datenbank —,
nimmt `lege_an` weiterhin Aufträge an, und jede Zeile trägt die
verschlüsselte Eingabedatei mit sich. Dazu ruft der Lifespan
`warteschlange.aufraeumlauf` **vor und unabhängig vom** Arbeiterstart; der
Aufräumlauf lebte sonst ausschliesslich im Arbeiterfaden, und ohne Arbeiter
räumte niemand. Beides zusammen genügt: Jeder Prozessstart räumt nach, und
ein Neustart kommt spätestens mit dem nächsten Deploy.

**`aufraeumlauf` ist die eine Fassung dieser zwei Schritte** — Arbeiterfaden
und Lifespan rufen dieselbe Funktion. Sie liegt in `warteschlange.py` und
nicht in `main.py`, weil der Arbeiter sie von dort nicht importieren könnte,
ohne einen Zirkel zu riskieren (`main` → Router → …, und der Lifespan
importiert seinerseits den Arbeiter); umgekehrt kennt `warteschlange` keinen
von beiden. Sie committet selbst, anders als der Rest des Moduls, der nur
`flush`t: Ein Aufräumdurchgang hat beim Aufrufer keine Fortsetzung.

**Und die Verdrahtung ist das, was zugesichert gehört, nicht der Helfer.**
Ein Test, der `aufraeumlauf` direkt aufruft, bleibt grün, während ihn niemand
ruft — derselbe Fehler wie 2026-08-25 beim Protokoll. Deshalb prüft
`test_lifespan_aufraeumen.py` mit einem `TestClient` (nur er durchläuft den
`lifespan`; `create_app()` allein tut es nicht), dass der Start sie ruft, und
`test_jobs_arbeiter.py`, dass der Arbeiter dieselbe Funktion benutzt.

**`raeume_ab` fasst `laeuft` niemals an** (`UNANTASTBAR`). Solange `expires_at`
nur an `fertig`/`fehler` haftete, war ein Abräumen ohne Statusfilter harmlos;
seit `lege_an` die Verfallszeit **bei der Anlage** setzt, trägt jede Zeile ab
Sekunde eins eine — auch die gerade bearbeitete. Ein Auftrag, der 24 h nach
seiner Anlage noch läuft, verlöre sonst seine Zeile mitten im Lauf, und das
`UPDATE` aus `melde_fertig` stürbe mit `StaleDataError` (ein DELETE auf eine
verschwundene Zeile warnt nur, ein UPDATE wirft). Drei Dinge hängen daran:

- **`wartend` bleibt ausdrücklich löschbar.** Ein Filter auf
  `fertig`/`fehler` wäre bequem und nähme genau den Zweck zurück, für den die
  Verfallszeit ab Anlage eingeführt wurde.
- **Der laufenden Zeile geht nichts verloren**, weil `gib_verwaiste_frei` im
  Aufräumlauf **davor** steht und eine hängengebliebene `laeuft`-Zeile bis
  dahin längst auf `wartend` oder `fehler` gesetzt hat. Die Reihenfolge im
  `aufraeumlauf` ist damit tragend, nicht Geschmack. Umgekehrt heisst das:
  Ohne den Freigabe-Schritt würde eine hängende Zeile *nie* abräumbar.
- **Ein Massenlöschen, keine Schleife über gelesene Zeilen** — sonst kann
  `hole_naechsten` im Fenster zwischen Lesen und Löschen eine `wartend`-Zeile
  zugeteilt haben. Dabei muss `synchronize_session="fetch"` stehen: Die
  Vorgabe `auto` wertet die Bedingung zusätzlich in Python aus, und SQLite
  gibt Zeitstempel **ohne** Zone zurück → `TypeError: can't compare
  offset-naive and offset-aware datetimes`. `Job` hat keine Beziehungen, es
  geht also kein ORM-Kaskadieren verloren.

**Der Herzschlag endet vor `melde_fertig`/`melde_fehler`, nicht danach.** Der
Takt (`schlage_herz`) läuft in einer *eigenen* Sitzung. Schriebe
`melde_fertig` die Auftragszeile, während er noch läuft, hielte der Durchgang
die Zeilensperre bis zu seinem `session.commit()` — und `stoppen()` wartete
die vollen fünf Sekunden seines `join` auf einen Faden, der auf uns wartet.
In `arbeiter._ein_durchgang` umschliesst der Herzschlag deshalb
ausschliesslich die Verarbeitung; ob sie gelang, wird erst danach
geschrieben. Das sieht nach Aufräumen aus Ordnungsliebe aus und ist keines.

**In `job.fehler` steht nur, was jemand bewusst formuliert hat.** Das Feld
geht unverändert in die Oberfläche und über `GET /jobs/{id}` auch an die
externe API. Bis zum 2026-08-26 stand dort `str(fehler)` jeder beliebigen
Ausnahme — der Wortlaut von lxml, openpyxl und python-docx. Nachgeprüft ist
das heute harmlos (die melden auf diesem Weg Formatfehler ohne
Dokumentinhalt), aber niemand kontrolliert diese Texte, sie ändern sich mit
jeder Version, und der OCR-Zyklus bringt Bibliotheken mit, die Dateipfade und
erkannte Textausschnitte in ihre Meldungen schreiben. Durchgereicht wird
deshalb nur noch, was in `arbeiter.DURCHREICHBAR` steht
(`NutzermeldungError`, `UnsupportedFormatError`); alles andere bekommt
`ALLGEMEINE_MELDUNG`, der volle Wortlaut steht mit `exc_info=True` im
Protokoll. **Die Umkehrung ist der Punkt:** Durchreichen muss beantragt
werden, Filtern ist der Normalfall — eine neu hinzugenommene Bibliothek ist
damit ab dem ersten Tag stumm. Wer im Auftragspfad einen Text für den
Menschen meint, wirft `NutzermeldungError` (`backend/jobs/ausfuehrung.py`),
kein nacktes `ValueError`.

**Der Auftragspfad zählt in der Nutzungsauswertung mit — unter denselben
Aktionsnamen wie der synchrone** (`anonymize_file`, `reverse_file`), nicht
unter eigenen. Ob eine Datei über der Schwelle lag, ist eine Frage des
Transportwegs und nicht der Nutzung; eigene Namen teilten dieselbe Tätigkeit
in `GET /api/v1/api-keys/usage` auf zwei Zeilen auf, die niemand wieder
zusammenrechnet. Der Aktor kommt aus `job.aktor`, nicht aus einem Request —
der läuft längst nicht mehr. Bis zum 2026-08-26 schrieb der Auftragspfad gar
keinen Eintrag: Die Zahlen sahen vollständig aus, es fehlten aber
ausgerechnet die grossen Läufe, und der Fehler wuchs mit der Dateigrösse.

**Der Fortschritt zählt Segmente, nicht Seiten — und der Rückruf war zwei
Zyklen lang tot.** `melde_fortschritt` gab es auf dem ganzen Weg vom
Arbeiter bis in die Pipeline, aber niemand rief ihn: Die erste Fassung
wartete auf eine Seitenzahl, die kein unterstütztes Format liefert (Seiten
entstehen in docx und xlsx erst beim Setzen und stehen gar nicht in der
Datei). `job.fortschritt` und `job.gesamt` standen damit dauerhaft auf
`null`, und ein grosser Lauf war zehn Minuten lang von einem
hängengebliebenen nicht zu unterscheiden — genau der Zustand, gegen den der
Auftragspfad gebaut wurde. Seit dem 2026-08-28 zählt
`pipeline.detect_in_segments` das **Segment**: die Einheit, die es wirklich
gibt, und die, an der die Arbeit hängt (je Segment ein spaCy-Lauf).

Vier Dinge, die dabei zusammenhängen:

- **Gezählt wird nur der Erkennungsdurchlauf.** KI-Schicht, Übertragung und
  Zurückschreiben laufen nicht je Segment; sie in denselben Balken zu
  pressen ginge nur mit einem geschätzten Nenner. Der Zähler steht deshalb
  eine Weile auf `gesamt`, bevor der Status auf `fertig` springt — die
  Oberfläche sagt in dieser Zeit weiter „wird verarbeitet". Ein Balken, der
  bei 60 % hängt, weil jemand geraten hat, wäre die schlechtere Lüge.
- **Auch das leere Segment wird gemeldet, vor dem `continue`.** Sonst bliebe
  der Zähler in einem Dokument mit vielen Leerabsätzen ohne Grund stehen,
  und ein Dokument, dessen letzte Segmente alle leer sind, erreichte
  `gesamt` nie.
- **Gedrosselt wird im Arbeiter, nicht in der Pipeline**
  (`FORTSCHRITT_TAKT_SEKUNDEN`, zwei Sekunden wie der Abfragetakt der
  Oberfläche). Der Rückruf kommt je Segment — bei einer grossen Tabelle
  zehntausendfach, und jede Meldung wäre sonst ein eigenes `UPDATE … COMMIT`.
  Wer ihn billig beantwortet, soll ihn vollständig bekommen; teuer ist er nur
  für uns. **`erledigt >= gesamt` geht immer durch**, sonst endete der Zähler
  auf einem beliebigen Zwischenstand und in der Oberfläche stünde bis zum
  Statuswechsel „Abschnitt 9'860 von 10'000".
- **Der Herzschlag bleibt trotzdem nötig.** Er sieht jetzt redundant aus —
  `melde_fortschritt` schreibt `updated_at` mit —, deckt aber genau die
  Phasen ab, die der Rückruf nicht kennt (siehe erster Punkt). Ein Auftrag
  darf nicht deshalb als verwaist gelten, weil er die Zählphase hinter sich
  hat.

**Die Auftragskennung überlebt ein Seitenneuladen — im Browser, nicht auf dem
Server.** Es gibt keine Liste eigener Aufträge, nur `GET /jobs/{id}`. Wer die
Seite dazwischen neu lud, verlor die Kennung ersatzlos; der Auftrag lief
weiter, war 24 Stunden abholbar und für niemanden mehr auffindbar.
`frontend/src/core/auftragsspeicher.ts` legt sie deshalb in `localStorage`
ab — nicht `sessionStorage`, das stirbt mit dem Tab, und genau der Fall ist
gemeint —, zusammen mit Art und Dateiname; Dokumentinhalte stehen dort nie.
Dass der Dateiname dort steht, ist eine **Abwägung** und keine
Unbedenklichkeit — `usage_events` bekommt ihn aus gutem Grund nicht.
Abgeräumt wird ein abgelaufener Eintrag seit dem 2026-08-26 beim **Start der
Anwendung** (`App.tsx` ruft `raeumeAbgelaufenenAuftragAb`) und nicht mehr nur
beim Lesen in einem der beiden Auftragsbereiche; wer das Werkzeug wieder
öffnet, räumt damit ab, gleich welcher Reiter oben steht. **Ein Rest bleibt
offen, und zwar unerreichbar:** Wer das Werkzeug nie wieder öffnet, bei dem
läuft in diesem Browserprofil kein Code von uns mehr. Abdeckung, Rest und die
geprüfte Alternative — `sessionStorage` für den Dateinamen allein, verworfen,
weil er am Tab hängt und damit genau in den gemeinten Fällen stürbe, und weil
Browser ihn beim Sitzungswiederherstellen zurückholen — stehen im Modulkopf
von `auftragsspeicher.ts`.

Gemerkt wird ausschliesslich in `api.verfolgeAuftrag` — beide Dateirouten und
beide Wiederaufnahmen gehen da durch, sonst vergässen die Fassungen
unterschiedlich. **Vergessen wird an zwei Stellen**: dort und im
„Zurücksetzen" von `Pseudonymisieren.tsx`, das den Eintrag ebenfalls
wegräumen muss (der Mensch hat den Lauf gerade verworfen). Beide geben dabei
die **Auftragskennung** mit, nicht die Art: Es gibt einen Speicherplatz, aber
zwei gleichzeitig montierte Bereiche (`App.tsx`), und ein bedingungsloses
`vergissAuftrag()` löschte den Auftrag des jeweils anderen — die dortige
Verarbeitung liefe weiter und wäre nach einem Neuladen unauffindbar, also
genau der Verlust, gegen den das Ganze gebaut ist. Nach der **Art** zu prüfen
deckte das ab, aber nicht zwei Läufe derselben Art; dass die während eines
Laufs gesperrte Oberfläche die heute verhindert, ist eine Absicherung in
einem anderen Modul und kann bei der nächsten Änderung an den Bedienelementen
wegfallen. Das „Zurücksetzen" kennt seine Kennung nicht — ein Auftrag
entsteht tief in `api.anonymisiereDatei` — und holt sie sich deshalb aus
`offenerAuftrag('anonymisieren')`, das nur den eigenen Bereich liefert. Zwei
weitere Dinge: Das Höchstalter des Eintrags muss zu
`warteschlange.AUFBEWAHRUNG` passen (beide 24 h), sonst
zeigt er auf einen längst entfernten Auftrag und die Wiederaufnahme endet
zuverlässig in einer 404. Und die `vault_id` eines wiederaufgenommenen
Rückführungs-Auftrags steht **nicht** im Speicher: Der Server hat sie bei der
Anlage in `parameter_json` festgehalten und gibt sie mit dem Ergebnis zurück
— im Browser hat sie nichts verloren.

**Die Wiederaufnahme darf nichts aus ihrer eigenen Closure lesen.** Der
Effekt in `Pseudonymisieren.tsx` hat `[]`-Deps und sieht damit den Stand des
Mounts: Kategorienwahl leer (der Katalog lädt erst danach), Modus auf dem
Vorgabewert. Übernahm er diese Werte als Stand des Laufs, brach „Anwenden"
mit „Wählen Sie mindestens eine Kategorie aus" ab, obwohl in der Oberfläche
eine angehakt war — und ein wiederaufgenommener `anonym`-Lauf legte dabei
einen Tresor an, also **Klarnamen in einer Zuordnungstabelle**, die niemand
bestellt hat. Kategorien, Modus und KI-Häkchen kommen deshalb aus dem
**Auftragsergebnis**: `ausfuehrung.py` legt sie als `requested_entities` und
`ki_pruefung` in den Umschlag (nicht als `entities` — der Schlüssel trägt
dort die Fundliste). Das macht die Antwort von `GET /jobs/{id}/result` zu
einer **Obermenge** der synchronen Antwort; wo im Baum „dieselbe Form" steht,
ist das seither zu eng. Fehlen die Felder (Auftrag von vor dem Deploy, bis zu
24 h lang möglich), bleiben sie `null` und „Anwenden" **verweigert mit einem
Text statt zu raten** — ein geratenes `umkehrbar` wäre wieder der unbestellte
Tresor.

**Die Eingabe wird bei jedem Ende gelöscht** — bei Erfolg *und* bei Fehler
(`warteschlange.melde_fertig`/`melde_fehler` setzen `eingabe_blob` beide auf
`None`). Auch ein gescheiterter Auftrag darf keine Personendaten
liegenlassen; das ist eine eigene Zusicherung mit eigenem Test, kein
Nebeneffekt.

**Das Auftragsergebnis ist ein verschlüsselter JSON-Umschlag, nicht das nackte
Dokument.** `ergebnis_blob` trägt Dokument (Base64), Fundliste, `applied` und
`vault_id` — die Form der synchronen Antwort, plus `requested_entities` und
`ki_pruefung` (siehe „Die Wiederaufnahme darf nichts aus ihrer eigenen
Closure lesen" oben), also eine Obermenge davon. Ohne die
Tresor-Kennung könnte ein umkehrbar pseudonymisiertes Dokument nie
zurückgeführt werden, weil die Zuordnung nur verschlüsselt in der Datenbank
liegt; ohne die Fundliste entfiele bei einer grossen Datei die menschliche
Prüfung auf Fehlalarme. Der Umschlag insgesamt bleibt so AES-GCM-verschlüsselt
wie zuvor die reinen Dokumentbytes — nur grösser.

**Neue Spalten am `Job`-Modell gehören in `NACHZUZIEHENDE_SPALTEN`** wie jede
andere Spalte auch (siehe „Schemaänderungen" oben) — hier besonders leicht zu
vergessen, weil `Job` schon lange existierte und eine neue Spalte daran wie
eine Erweiterung eines längst produktiven Modells aussieht, nicht wie ein neues.

## PDF und OCR (Phase 6)

Ein PDF ist kein monolithes Format — es kann beides sein: ein am Rechner
erzeugtes Dokument mit Textebene (der Text steht als solcher im Dokument, mit
Positionsangaben), oder ein eingescanntes Bild je Seite, wo nur das Pixel
steht. Phase 6 implementiert deshalb **zwei Staffeln**: Die Textebene (der
Normalfall) läuft immer, OCR für Scans kommt danach nur, wenn es verfügbar
ist — alles über denselben `anonymize-file`/`reverse-file`-Pfad der
asynchronen Aufträge, denn sowohl die Rasterung wie der OCR-Lauf sind
speicher- und zeitintensiv.

**Das Design ist Redaction, nicht formaterhaltener Glyphenaustausch.** Die
Stelle, an der ein Name erkannt wird, wird aus dem PDF **entfernt** (nicht nur
optisch überdeckt). Das Layout der Seite bleibt erhalten, die geschwärzte
Stelle ändert sich. Für ein Datenschutzwerkzeug ist das die richtige
Semantik — es geht nicht darum, dass das Ergebnis hübsch aussieht, sondern
dass der Klarname wirklich weg ist. Das formaterhaltende Neusetzen bleibt
ausdrücklich „ein eigenes Vorhaben" und gehört nicht hierher.

Zwei Grenzen, die dabei massgeblich sind:

1. **Die tragende Sicherheitsregel: Ein Scan ohne lesbaren Text scheitert
   laut, nie stumm.** Eine gescannte Seite ohne Textebene ist für den lokalen
   Erkenner unsichtbar — OCR würde sie lesbar machen. Ist OCR aber nicht
   verfügbar (Schalter aus oder Binary fehlt), liefert ein naiver Adapter
   keine Funde. Ein Dokument ohne Funde sieht pseudonymisiert aus, obwohl der
   volle Klarname als Bild darin steht — das ist das gefährlichste Fehlerbild
   dieses Werkzeugs. Deshalb wirft der Adapter `NutzermeldungError` mit
   klarem Text: „Dieses PDF enthält gescannte Seiten; die Texterkennung ist
   auf dieser Installation nicht aktiviert. Bitte als DOCX/TXT hochladen oder
   die Administration kontaktieren." Ein halb erkanntes Dokument ist
   gefährlicher als gar keines — wer diesen Text sieht, trifft bewusst eine
   Entscheidung statt den fehlgeschlagenen Lauf misszudeuten.
2. **OCR läuft lokal (Tesseract auf dem Knoten), nicht in der Cloud.** Das ist
   ganz bewusst: Ein Cloud-OCR-Dienst verschöbe den gesamten
   Dokumentinhalt nach aussen und stünde im Widerspruch zur
   Grundaussage des Werkzeugs. Tesseract bleibt auf dem Knoten,
   kein Byte verlässt den Server. Das ist auch der Grund, warum OCR hinter
   einem Schalter liegt: Das Systembinary `tesseract` und die Sprachdaten `deu`
   sind nicht über pip installierbar, sondern eine **Deploy-Entscheidung** des
   Knotenbetreibers. Wer den Pseudonymisierer ohne Tesseract betreibt, bei dem
   läuft die textbasierte Erkennung vollständig; er verzichtet bewusst auf
   Scans, statt sie stillschweigend unverarbeitet durchzulassen.

**Der Adapter (`backend/adapters/pdf_adapter.py`)** verwaltet beide Stufen:
Ein Segment ist eine Zeile, und die Location vermerkt die Quelle (`text` oder
`ocr`). `extract_segments` prüft die Textebene, und wenn eine Seite keinen
nennenswerten Text trägt (Schwelle z. B. < 10 Zeichen), rastert sie der
Adapter zu einem Bild und schickt es durch Tesseract — wenn verfügbar und
aktiviert. Die Ersetzung (Redaction) ist **einheitlich**: jeder Fund, ob von
der Textebene oder von OCR, wird gelöscht (aus dem Inhaltsstrom des PDF, nicht
nur überdeckt), und das Pseudonym wird darüber gestempelt.

**Metadaten und Determinismus.** Der Adapter leert Autor, Titel und XMP-Daten
(das alles kann Originalpersonendaten tragen). Ein leeres `replacements`-Feld
führt zum Roundtrip: das PDF wird neu gespeichert und ist inhaltsgleich (nicht
unbedingt byte-gleich, aber das ist beim PDF nichts Ungewöhnliches).

**Zwei Fehlerklassen, zwei Antworten — und keine `500`.** Ein gescanntes PDF
ohne aktivierte OCR ist kein Format-, sondern ein Installationsproblem: Der
Adapter *könnte* es lesen, läge Tesseract auf dem Knoten. Er wirft dafür
`NutzermeldungError` (nicht `UnsupportedFormatError` — das bleibt dem wirklich
nicht unterstützten Format vorbehalten; eine beschädigte PDF-Datei wirft
ihrerseits `ValueError` aus `_open`). Beide Pfade übersetzen die
`NutzermeldungError` sauber: der synchrone in eine `415` mit dem erklärenden
Text, der Auftragspfad in einen gescheiterten Auftrag mit derselben Meldung
(der Arbeiter lässt sie über `DURCHREICHBAR` an die Oberfläche) — nie in eine
`500` mitten in der Anfrage.

**Eine Falle steckt dabei in der Reihenfolge.** `documents._laeuft_als_auftrag`
ruft `extract_segments`, um Zeichen zu zählen (die Auftragsschwelle, siehe
„Asynchrone Aufträge"), **bevor** über synchron/asynchron entschieden ist. Für
einen kleinen Scan ohne OCR wirft der Adapter genau dort — und genau dort fängt
der Router seit dem 2026-08-28 auch `NutzermeldungError`, nicht nur
`UnsupportedFormatError`; sonst entwiche die Ausnahme als `500`. Der Wächter ist
`test_gescanntes_pdf_ohne_ocr_endet_mit_415_nicht_500`. Ein **grosser** Scan
über der Byte-Schranke erreicht diese Stelle nicht: Er wird ohne Öffnen zum
Auftrag und scheitert erst im Arbeiter — dort sauber als `fehler` mit Text.

**Speicher und Zeit binden PDF an den Auftragspfad.** Eine gescannte Seite
wird zu einem Bild gerastert (die DPI, `OCR_DPI`, ist der Hebel für den
Speicher — gemessen ~1.1 GB Spitze je OCR-Lauf), und Tesseract braucht rund
2 Sekunden je Seite; eine dicke Akte sprengt jedes offene Zeitfenster des
Balancers. Der Arbeiter fährt bewusst **einen** Job je Worker (siehe
`arbeiter.py`), genau dafür. Lokal ohne Tesseract prüfen: ein kleines
Textebenen-PDF (unter beiden Schwellen) läuft synchron und ohne OCR-Haken
durch; ein gescanntes bekommt bewusst die `415` von oben.

## Betriebs-Eigenheiten von Infomaniak jCloud

- **Env-Variablen nur über das Dashboard, danach Dashboard-Restart.** Ein
  Prozess-Restart lädt sie nicht neu.
- Der Jelastic-API-Token ist **methoden-beschränkt** auf `Vcs.Update` +
  `RestartNodes`. `execcmd`, `AddContainerEnvVars` und Log-Lesen liefern
  `result 8202`. Deshalb läuft der Build-Schritt über den SSH-Gate.
- Der LB reicht **Port 80 unverschlüsselt** durch und setzt `X-Forwarded-Proto`
  nur bei HTTPS. Der Redirect darf sich nie allein auf den Header verlassen.
- Ein Deploy hat **~3–6 min Ausfall in zwei Fenstern**, mit einer trügerischen
  200 dazwischen. Smoke-Tests dürfen nicht beim ersten Erfolg aufhören.
- **Der NGINX-Balancer ist der Eintrittspunkt**: dort liegt die öffentliche
  IPv4, dort terminiert SSL, von dort geht es an den Anwendungsserver. Am
  2026-08-18 nachgemessen — alle Schwestertools antworten mit `Server: nginx`,
  obwohl ihre Apps Express sind. Eine ältere Projektnotiz behauptete das
  Gegenteil; sie war falsch.
- Das `venv` liegt **ausserhalb** des Deploy-Roots, damit es VCS-Updates überlebt.
- **Uvicorn konfiguriert nur seine eigenen Logger.** Ein Logger ohne Behandler
  fällt auf Pythons `lastResort` zurück, und der gibt **nur WARNING und höher**
  aus. Am 2026-08-25 gemessen: `logger.info` der Anwendung stand null Mal im
  Protokoll — die Messreihe zu Ort und Organisation hat deshalb seit ihrem
  Deploy nichts gesammelt. `richte_protokoll_ein()` in `backend/main.py` hängt
  darum einen Behandler an den Namensraum `pti` und setzt die Stufe aus
  `LOG_LEVEL`. Zwei Folgerungen für neuen Code: **jeder Logger heisst `pti.…`**,
  nie `__name__` (sonst `backend.…`, und die Konfiguration greift nicht) — und
  `LOG_LEVEL` muss in `deploy/pythonenv` **exportiert** sein, sonst sieht der
  Dienstprozess die Dashboard-Variable nicht. Beides bewacht
  `backend/tests/test_protokoll.py`.

## Nachbar-Repos — Vorbilder, keine Abhängigkeiten

`d:/VS Code Projekte/` — `pti-crm`, `landing`, `pti-translator`, `pti-ppm`,
`mvp-sandbox`.

**Sie sind Nachbarn, kein Verbund.** Wo im Baum ein anderes Werkzeug
auftaucht, ist es entweder ein *Vorbild* (`pti-translator` für Deploy, SSO
und den `X-Api-Key`-Kopf — portiert, nicht aufgerufen) oder eine Angabe zum
*Betrieb* (alle liegen hinter demselben NGINX-Balancer, siehe
`JELASTIC_SETUP.md`). Keines davon ist eine Laufzeit-Abhängigkeit, und keine
Änderung hier braucht eine Absprache mit `pti-ppm` oder `pti-translator`.

Die Datenbank ist ohnehin getrennt: Der Pseudonymisierer hält seine eigene,
nicht die von CRM und PPM (`JELASTIC_SETUP.md`).

### Die zwei echten Verbindungen nach aussen

Damit „unabhängig" nicht zur Behauptung wird — es gibt genau zwei, und beide
sind bekannt:

1. **`pti-crm` als Identity-Provider** (Laufzeit, unvermeidbar in der
   heutigen Bauform). Das SSO-Gate fragt server-zu-server
   `GET {CRM_BASE_URL}/api/auth/session` und **fail-closed**: CRM nicht
   erreichbar → niemand kommt in die Oberfläche. Das ist der Preis dafür,
   *keine* eigene User-DB und kein Token-Secret zu halten — die Alternative
   wäre ein zweiter Ort für Personendaten und Passwörter, und den will
   dieses Werkzeug ausdrücklich nicht.
   **Der Maschinenpfad `/api/ext/v1` hängt nicht daran**: Er prüft
   gehashte Schlüssel aus der eigenen Datenbank und läuft auch dann, wenn
   das CRM steht. Wer den Pseudonymisierer wirklich ohne CRM betreiben
   will, betreibt ihn über diesen Pfad; lokal tut es `HUB_AUTH=off`.
2. ~~`scripts/behoerden_aus_glossar.py` liest aus einem Nachbar-Repo~~ —
   **am 2026-08-28 aufgelöst.** Bis dahin zeigte die Vorgabe auf
   `../pti-translator/backend/data/glossaries.json`; das war die einzige
   Stelle im ganzen Baum, an der dieses Werkzeug ein anderes PTI-Projekt
   tatsächlich angefasst hat. `--quelle` ist jetzt ein **Pflichtargument**,
   und das Skript kennt keinen Ablageort mehr. Die Herkunftsangabe, die es
   in jeden Eintrag schreibt, kommt über `--quellenname` ebenfalls vom
   Aufrufer statt aus dem Code.

Damit bleibt **`pti-crm` die einzige Verbindung.** Alles andere, was danach
aussieht, ist keine:

| Stelle | Was es ist |
|---|---|
| `core/api_key_auth.py`, `hub_access_auth.py`, `https_redirect.py` | **Portierungen** — Code, der anderswo abgeschaut wurde. Kein Aufruf. |
| `frontend/src/components/Logo.tsx` | **Brand-Asset**, in allen PTI-Werkzeugen dasselbe. |
| `test_hub_access_auth.py` (`"apps": {…, "translator": False}`) | Prüft die **Abgrenzung**: Zugriff auf ein anderes Werkzeug öffnet dieses hier nicht. |
| `JELASTIC_SETUP.md` | **Betriebsumgebung** — alle Werkzeuge hinter demselben NGINX-Balancer. Eigene Datenbank, eigener Knoten. |
| `scripts/data/behoerden.json` (`"quellen": ["PTI Glossar"]`) | **Herkunft der Daten**, nicht des Systems. Nennt seit dem 2026-08-28 kein Repo mehr. |

Diese Nennungen bleiben absichtlich stehen: Sie erklären, *warum* Code so
aussieht, wie er aussieht. Sie zu tilgen nähme dem nächsten Leser die
Begründung und schaffte keine einzige Kopplung ab, weil es nie eine war.

### Der Grundsatz für neue Arbeit

**Der Pseudonymisierer wird als eigenständiges Modul gebaut.** Schnittstellen
nach aussen — ja, und zwar bewusst gepflegt (`API.md`). Eine Abhängigkeit zu
einer anderen Anwendung — nein, auch nicht stillschweigend über ein geteiltes
Schema, eine geteilte Datenbank oder einen Direktaufruf. Wer eine solche
Kopplung erwägt, entscheidet das ausdrücklich mit dem Auftraggeber und trägt
sie hier ein; sie entsteht nicht nebenbei.

Konkret heisst das auch: **Kein Text im Baum benennt eine andere Anwendung
als gesetzten Abnehmer der externen API.** Sie ist ein Angebot, keine
Verabredung — wer einen gültigen Schlüssel hat, ruft auf.
