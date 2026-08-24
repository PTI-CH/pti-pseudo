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
<https://pti-tip-hub.ch/ideen/pseudonymizer.html>. Diese Etappe setzt **Phase 1**
daraus um (§12): Engine + GUI, lokal, ohne Async-Jobs und ohne LLM-Schicht.

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
- **Bezeichner englisch:** `pseudonymizer` — Subdomain, Kachel-ID, `AppKey`,
  DB-Spalte `pseudonymizer_access`. Nur das Repo heisst deutsch.
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

1. **Nur Fenster verlassen das Haus** — rund 20 Wörter um jeden Fundkandidaten,
   nie ein ganzes Segment. Preis: Ein vollständig übersehener Name bleibt
   übersehen. Trägt trotzdem, weil `propagate_findings` **nach** der KI läuft
   und den korrigierten vollständigen Namen durchs Dokument trägt — deshalb ist
   die Reihenfolge in `detect_in_segments` nicht beliebig.
2. **Bei Ausfall weiterlaufen**, nicht scheitern. Die lokale Erkennung ist mit
   95 % Namen-Recall der Normalbetrieb.
3. **Nur erweitern, nie verwerfen** — dieselbe Regel wie bei
   `propagate_findings`. Ein zu Unrecht verworfener Fund kostet einen
   Klarnamen, und zwar unsichtbar.
4. **Vorgabe aus auf API-Ebene**, vorangehakt in der Oberfläche. Sonst begänne
   `/api/ext/v1` ab einem Deploy stillschweigend, Inhalte hinauszuschicken.

Drei am echten Dienst gemessene Eigenheiten stecken in
`backend/ki/infomaniak.py`: `response_format: json_object` ergibt **HTTP 400**
(nur `json_schema` geht), **HTTP 200 mit leerem `content` ist ein Fehler** (vier
Modelle antworten so bei knappem Token-Budget), und die **Produkt-Id steht im
Pfad**, nicht im Kopf.

Das Modell ist `Ministral-3`, nicht Apertus: Massenklassifikation, nicht Chat.

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

Alembic bleibt das richtige Werkzeug für alles jenseits additiver Spalten und
fehlt bislang. Es auf einer laufenden Produktionsdatenbank einzuführen heisst,
den Bestand korrekt auf eine Basisversion zu stempeln — ein Schritt, der nicht
nebenbei mit einer Funktionsänderung ausgeliefert werden sollte.

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

## Schwester-Repos

`d:/VS Code Projekte/` — `pti-crm` (Identity-Provider, Schema-Owner),
`landing` (Hub mit Kachel-Grid), `pti-translator` (nächstes Vorbild für Deploy
und SSO), `pti-ppm`, `mvp-sandbox`.
