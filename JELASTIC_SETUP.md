# Jelastic-Einrichtung — pti-pseudonymisierer

Betriebs-Runbook für die Infomaniak-jCloud-Umgebung. Aufbau nach dem Vorbild von
`pti-translator/JELASTIC_SETUP.md`, aber mit einem entscheidenden Unterschied:
**dies ist der erste Python-Knoten im PTI-Bestand.** Alle Schwestertools
(`pti-crm`, `pti-ppm`, `pti-translator`, `mvp-sandbox`, `landing`) laufen auf
Node.js mit pm2. Es gibt also kein Hauswissen über dieses Template — der
Recon-Spike in Abschnitt 0 ist deshalb Pflicht und kein Formalismus.

---

## 0. Node-Inventar

> **Erhoben am 2026-08-18** per SSH auf Knoten 213705. Damit sind die Annahmen
> aus dem Node.js-Muster durch Messwerte ersetzt.

```bash
ssh -i ~/.ssh/jelastic_ci -p 3022 213705-10792@gate.jpc.infomaniak.com
```

Der Benutzername ist `<Knoten-ID>-<Konto-ID>`. Der blosse Konto-Benutzer
(`10792`) landet in einer Auswahl-Shell und taugt weder fuer `ssh -c` noch fuer
rsync.

| Frage | Befund |
|---|---|
| Umgebung | `pti-pseudo`, Genf DC3 |
| Knoten | Balancer **213781** (NGINX 1.30.4, oeffentliche IPv4 185.172.103.24) · App **213705** (Python Engine 3.14.7) · PostgreSQL **213680** (18.4) |
| Deploy-Root | **`/home/jelastic/ROOT`** (`metainf.conf: ROOT_DIR`) |
| Python | **3.14.7**, `/bin/python`, prefix `/opt/jelastic-python` |
| Prozessmanager | **kein supervisord.** `systemd: pythonengine.service` → `/usr/local/sbin/process-manager` |
| Startbefehl | `python -m uvicorn $APP_MODULE --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*` |
| Konfiguration | **`/home/jelastic/pythonenv`** — `APP_MODULE`, `WORKERS`, `SSLEnabled`, `PROCESS_MANAGER`, `HOST`, `PORT` |
| `npm` | **nicht vorhanden** → Frontend-Build gehoert in die Action |
| Paketziel | `pip install --user` → `/home/jelastic/.local/lib/python3.14/site-packages` (user site ist aktiv) |
| VCS-Deployment | **nicht moeglich** (siehe unten) |
| `client_max_body_size` | **100m** am Balancer — reicht fuer 25-MB-Uploads |
| Ausgang | PyPI und GitHub erreichbar (HTTP 200) |
| Platte / RAM | 43 GB frei von 48 GB · 3 GB RAM |
| `sudo` | nein |

### Zwei Voreinstellungen, die zu 502 fuehren

Die frisch angelegte Umgebung antwortete mit 502. Beides nachgemessen, beides
in `deploy/pythonenv` behoben:

1. **`SSLEnabled="on"`** laesst uvicorn TLS auf Port 8000 sprechen, der
   Balancer proxyt aber per `proxy_pass http://`. Beweis vom Balancer aus:
   `http://10.100.25.61:8000/` → leere Antwort, `https://…` → 200.
2. **`WORKERS` = `nproc` = 5.** Jeder Worker laedt sein eigenes spaCy-Modell
   (411 MB), und uvicorn startet Worker per multiprocessing-*spawn*, nicht per
   fork — es gibt also kein Copy-on-Write. 5 Worker waeren rund 2 GB bei 3 GB
   Knotenlimit.

### VCS-Deployment ist bei diesem Knotentyp nicht verfuegbar

Der Panel-Eintrag „Veroeffentlichungen" ist ausgegraut: die Funktion gehoert zu
den *verwalteten* Anwendungsservern (Apache Python), nicht zum nackten
Interpreter-Container. Der API-Aufruf bestaetigt es:

```
POST vcs/rest/update  envName=pti-pseudo  context=ROOT
→ {"result":2500,"error":"context [ROOT] doesnt exist in nodeGroup [{1}]"}
```

Der Code kommt deshalb **per rsync ueber den SSH-Gate**. Das ist kein Notnagel,
sondern hat einen echten Vorteil: auf dem Knoten liegt damit **kein
GitHub-Token**. Ein Angreifer auf dem Anwendungsserver findet dort kein
Zugangsdatum zu den Repositories.

---

## 1. Umgebung bestellen

**Dies ist der Weg: drei Knoten, voll funktionsfähig** (Abschnitt 1a). Die
Ein-Knoten-Variante in 1b ist nur dokumentiert, falls einmal schnell und
billig etwas zum Anschauen gebraucht wird — sie ist ausdrücklich **nicht** der
geplante Betriebszustand.

### 1a. Regelbetrieb — drei Knoten

Environment-Name **`pti-pseudonymisierer`**, Region **Geneva DC3**.

| Rolle | Stack | Reserviert | Skalierungslimit |
|---|---|---|---|
| Balancer | NGINX | 1 Cloudlet | 4 |
| Application | **Python 3.11 / 3.12** | 6 Cloudlets (768 MiB) | **24 (3 GiB)** |
| SQL | PostgreSQL 16 oder 18 | 2 Cloudlets | 8 |

Kein Redis: Phase 1 verarbeitet synchron, die asynchrone Warteschlange kommt
erst in Phase 2. Das spart einen Knoten.

### Warum 24 Cloudlets Limit

Gemessen am 2026-08-18 auf dem Entwicklungsrechner:

| | RSS |
|---|---|
| Python nackt | 18 MB |
| + Presidio importiert | 101 MB |
| + `de_core_news_md` geladen | **406 MB** |
| + Analyse von 21'000 Zeichen | 418 MB |

Ladezeit des Modells 1.1 s (Engine gesamt 7.1 s), Durchsatz ~16'000 Zeichen/s.

Zwei Worker mit `preload_app=True` teilen sich die Modellseiten via
Copy-on-Write, dazu kommt der transiente lxml-Baum grosser DOCX-Dateien:
rund **1 GB** im Normalbetrieb, Spitzen bis 1,5 GB. Das grosse Modell
`de_core_news_lg` liegt bei etwa dem Doppelten. Das Limit ist deshalb gleich für
`lg` bemessen, obwohl Phase 1 mit `md` startet — ein späterer Modellwechsel soll
kein Re-Sizing brauchen. Infomaniak rechnet oberhalb der Reservierung
nutzungsbasiert ab, ein hohes Limit kostet im Leerlauf also nichts.

### 1b. Rückfallvariante — ein Knoten

Nicht der geplante Betriebszustand. Festgehalten, weil die Anwendung es kann und
es einmal nützlich sein könnte (schneller Blick, Nebenumgebung, Ausfall des
PostgreSQL-Knotens).

Ein Python-Knoten, 4 Cloudlets reserviert, Limit 16. Kein Balancer, kein
PostgreSQL. Der `infomaniak`-Skill der Schwesterprojekte sagt dasselbe:
Lastenausgleich „erst bei hohem Traffic", SQL-Knoten „separat hinzufügen".

Der Tresor läuft dann auf SQLite — am 2026-08-18 mit vollständigem Durchlauf
inklusive verlustfreier Rückführung verifiziert:

```
DATABASE_URL=sqlite:////home/jelastic/pti-pseudonymisierer.db
WEB_CONCURRENCY=1
```

Vier Schrägstriche — drei fürs Schema, einer für den absoluten Pfad. Die Datei
muss **ausserhalb** des Deploy-Roots liegen, aus demselben Grund wie das venv:
ein VCS-Update ersetzt `ROOT`. `WEB_CONCURRENCY=1` halbiert den Speicherbedarf
auf rund 500 MB.

Ohne Balancer beim Domain-Binden den **Shared-SLB-Weg** wählen (Abschnitt 2,
Variante B): `pseudo` ist eine Subdomain unter `.ch`, dort ist ein CNAME
erlaubt — kostenlos, ohne dedizierte IPv4. Der A-Record-Weg der Schwestertools
verlangt dagegen den Balancer, weil Jelastic bei einer rohen Public-IP kein
`:80 → :8000` routet.

Was man sich damit einhandelt:

| | |
|---|---|
| Schreibzugriff | SQLite kennt nur einen Schreiber |
| Sicherung | kein `pg_dump`; Ersatz ist ein Cron, der die Datei kopiert |
| Rückweg zu PostgreSQL | **kein blosses Umschalten von `DATABASE_URL`** — die UUID-Spalten rendern auf SQLite als `CHAR(32)`, auf PostgreSQL nativ. Es braucht ein Umzugsskript. |

## 2. Netzwerk und Domain

1. **Öffentliche IPv4 auf den NGINX-Balancer** — er ist der Eintrittspunkt,
   terminiert SSL und leitet an den Anwendungsserver weiter.

   Am 2026-08-18 an den laufenden Schwestertools nachgemessen, weil eine ältere
   Projektnotiz das Gegenteil behauptete („SLB auf dem Anwendungsserver, nicht
   am LB"): `crm`, `ppm`, `translator`, `mvp-sandbox` und der Hub antworten
   **alle** mit `Server: nginx`, obwohl ihre Anwendungen Express sind — Express
   sendet diesen Header nicht. Jede Subdomain hat eine eigene dedizierte IPv4
   (A-Record, kein Shared-CNAME). Dieselbe Notiz nennt `83.228.254.189`
   ausserdem selbst „LB-IP" und schreibt „SSL = Let's Encrypt am LB". Die
   Behauptung war falsch; der Balancer ist der Eintrittspunkt.
2. **DNS** für `pseudo.pti-tip-hub.ch`: **A-Record auf die dedizierte
   IPv4 des Balancers**, kein CNAME. Das ist das Muster aller Schwestertools,
   am 2026-08-18 verifiziert:

   | Host | A-Record |
   |---|---|
   | `crm.pti-tip-hub.ch` | 83.228.254.154 |
   | `ppm.pti-tip-hub.ch` | 83.228.254.90 |
   | `translator.pti-tip-hub.ch` | 83.228.252.106 |
   | `mvp-sandbox.pti-tip-hub.ch` | 83.228.252.38 |
   | `pti-tip-hub.ch` (Hub) | 83.228.254.189 |

   Jede Umgebung hat also ihre eigene öffentliche IPv4. IPv6 läuft über einen
   Wildcard-AAAA auf Infomaniaks Shared-LB `2001:1700:a00::14`.
3. **Custom Domain binden**, danach **SSL** (Let's Encrypt) für die eigene Domain.
4. Erst wenn das Zertifikat gültig ist, die `LIVE_URL` in `deploy-prod.yml`
   scharfstellen. Die alte jcloud-URL bekommt danach ein nicht mehr passendes
   Zertifikat — beim Translator hat das den Smoke-Test fälschlich scheitern lassen.

## 3. Datenbank

> Nur bei der Rückfallvariante (1b) überspringen — dort läuft der Tresor auf
> SQLite und es gibt keinen PostgreSQL-Knoten.

Eigene Datenbank `pti_pseudonymisierer` mit eigenem Benutzer auf dem
PostgreSQL-Knoten dieser Umgebung.

**Bewusst nicht an die geteilte `pti-db` angeschlossen.** Der Vault enthält die
verschlüsselten Originale aller pseudonymisierten Personendaten — dieser Bestand
gehört nicht in dieselbe Datenbank wie CRM und PPM. Das Tool ist DB-isoliert wie
`hika-assistent` und `pti-translator`.

Jelastic-PostgreSQL erzwingt SSL (`hostssl` in `pg_hba.conf`), die
`DATABASE_URL` braucht deshalb `?sslmode=require`.

## 4. Git verbinden — entfaellt

Es gibt nichts zu verbinden. Siehe Abschnitt 0: der Knotentyp unterstuetzt kein
VCS-Deployment, und der Code kommt per rsync aus der GitHub-Action.

Damit entfaellt auch der fine-grained PAT, den ein VCS-Dialog gebraucht haette.
Die Action authentisiert sich ausschliesslich mit dem SSH-Schluessel
(`JELASTIC_SSH_KEY`), der ohnehin fuer den Build-Schritt noetig ist.

## 5. Umgebungsvariablen

Vollständige, kommentierte Liste in `.env.example`. Zu setzen sind mindestens:

```
ENV=production
PORT=8000
DATABASE_URL=postgresql+psycopg://…?sslmode=require
CRM_BASE_URL=https://crm.pti-tip-hub.ch
HUB_BASE_URL=https://pti-tip-hub.ch
HUB_APP_KEY=pseudonymizer
RELAY_SECRET=…                      identisch zu CRM und Hub
PUBLIC_BASE_URL=https://pseudo.pti-tip-hub.ch
VAULT_MASTER_KEY=…                  openssl rand -hex 32
SPACY_MODEL=de_core_news_md
MAX_UPLOAD_BYTES=26214400
```

> **`VAULT_MASTER_KEY` geht verloren = alle Vaults sind unwiderruflich unlesbar.**
> Der Schlüssel gehört zusätzlich in den Passwortmanager, nicht nur ins Panel.

**Nach jeder Variablenänderung ist ein Dashboard-Restart nötig.** Ein
Prozess-Restart übernimmt die Umgebung nicht — dieselbe Falle wie bei pm2 auf
den Node-Knoten.

Werte, die man nicht ins Panel tippen möchte, können alternativ in eine Datei
unter `/home/jelastic/pti.env` (Pfad über `ENV_FILE`). Sie liegt **ausserhalb**
des Deploy-Roots und überlebt damit jedes VCS-Update.

## 6. Erstinstallation auf dem Knoten

```bash
python3 -m venv /home/jelastic/venv
/home/jelastic/venv/bin/pip install --upgrade pip
/home/jelastic/venv/bin/pip install -r /home/jelastic/ROOT/pti-requirements.txt
/home/jelastic/venv/bin/python -c "import de_core_news_md; print('Modell OK')"
```

Das venv liegt **absichtlich ausserhalb** des Deploy-Roots. Ein VCS-Update
ersetzt den Inhalt von `ROOT`; läge die rund 900 MB grosse Umgebung darin, würde
sie bei jedem Deploy neu aufgebaut. So dauert ein Deploy Sekunden statt Minuten.

Prozessmanager einrichten:

```bash
cp /home/jelastic/ROOT/deploy/supervisord.conf /etc/supervisord.d/pti-pseudonymisierer.ini
supervisorctl reread && supervisorctl update
supervisorctl status pti-pseudonymisierer
```

## 7. Deploy

Regelfall: Push auf `main` → GitHub-Action. Ablauf und Secrets stehen in
`.github/workflows/README.md`.

Rückfall von Hand: `.\scripts\deploy-prod.ps1`.

### Die wichtigste Eigenheit dieses Deploys

Der Python-Knoten hat **kein npm**. Das Frontend wird im GitHub-Runner gebaut
und per `scp` hochgeladen.

> **Ein Panel-Klick „Update from Repository" aktualisiert das Frontend NICHT.**
> Er holt nur den Quellcode. Nach einem manuellen Panel-Deploy immer die Action
> nachziehen (`gh workflow run deploy-prod.yml`), sonst läuft eine alte
> Oberfläche gegen ein neues Backend — und niemand sieht es sofort.

### Was beim Deploy normal ist

Ein Jelastic-Restart erzeugt **~3–6 Minuten Ausfall in zwei Fenstern**, mit einer
trügerischen erfolgreichen Antwort dazwischen. Der Smoke-Test wartet deshalb 30 s
und versucht es zwölfmal. Nicht beim ersten grünen Ergebnis für fertig erklären.

## 8. Fehlersuche

| Symptom | Wahrscheinliche Ursache |
|---|---|
| 404 auf `/`, aber `/api/health` antwortet | `frontend/dist` fehlt auf dem Knoten — Panel-Deploy ohne Action |
| Alle Routen ausser Health liefern 500 | Modell fehlt oder `VAULT_MASTER_KEY`/`DATABASE_URL` nicht gesetzt |
| Gegatete Route liefert 200 ohne Login | `HUB_AUTH=off` steht versehentlich in der Prod-Env |
| Alles redirected, auch `/api/health` | HTTPS-Redirect greift zu früh — Ausnahme für Health prüfen |
| 413 beim Upload | `client_max_body_size` im Knoten-NGINX steht auf dem 1-MB-Default |
| Prozess startet, stirbt nach Sekunden | `startsecs` in supervisord verdeckt eine Absturzschleife — `supervisorctl tail … stderr` |
| Variablenänderung wirkt nicht | Prozess- statt Dashboard-Restart gemacht |
| `result: 8202` von der API | Token-Methode nicht freigeschaltet (nur `Vcs.Update` + `RestartNodes`) |

Logs: `supervisorctl tail -f pti-pseudonymisierer stderr`, ersatzweise
`/var/log/pti-pseudonymisierer.err.log`. Der Deploy-Token darf **keine** Logs
über die API lesen — dafür die Web-SSH-Konsole im Dashboard nutzen.

## 9. Checkliste vor der ersten Freigabe

Gilt für beide Ausbaustufen:

- [ ] Node-Inventar (Abschnitt 0) ausgefüllt, Pfade in `deploy-prod.yml` korrigiert
- [ ] `JELASTIC_APP_NODE_ID` gesetzt (nicht mehr `REPLACE_ME`)
- [ ] GitHub-Secrets gesetzt
- [ ] Umgebungsvariablen gesetzt, `VAULT_MASTER_KEY` im Passwortmanager gesichert
- [ ] DNS + Zertifikat für `pseudo.pti-tip-hub.ch` gültig
- [ ] `client_max_body_size` ≥ 25 MB
- [ ] Prozessmanager läuft, Neustart des Knotens überstanden
- [ ] `/api/health` liefert `{"ok": true}`
- [ ] Anonymer Aufruf von `/` leitet auf den Hub um
- [ ] `pseudonymizer_access` im CRM gesetzt, Zugriff als berechtigter Nutzer geprüft

Zusätzlich beim Regelbetrieb (1a):

- [ ] `?sslmode=require` in der `DATABASE_URL`
- [ ] Tresor überlebt einen Deploy: Vault anlegen → deployen → Rückführung geht noch

Nur falls die Rückfallvariante (1b) zum Einsatz kommt:

- [ ] `DATABASE_URL` zeigt auf einen Pfad **ausserhalb** von `ROOT`
- [ ] `WEB_CONCURRENCY=1`
- [ ] Sicherungs-Cron für die SQLite-Datei eingerichtet
