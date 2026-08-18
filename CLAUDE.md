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
durch) und das TIP-Hub-SSO-Gate. Die Engine (`backend/engine/`) baut auf der
Presidio-`AnalyzerEngine` auf, mit deutschem spaCy-Modell und eigenen Schweizer
Erkennern. Der Vault (`backend/vault/`) hält die Zuordnung Original→Pseudonym
AES-GCM-verschlüsselt in PostgreSQL, damit `documents:reverse` funktioniert.

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
