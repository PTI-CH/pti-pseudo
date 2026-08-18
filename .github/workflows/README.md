# GitHub-Actions — Einrichtung

`deploy-prod.yml` deployt bei jedem Push auf `main` nach Infomaniak jCloud.
Die übrigen sechs Workflows in diesem Verzeichnis stammen aus der Presidio-Codebasis
und sind **serverseitig deaktiviert** — nicht gelöscht, weil das bei jedem
`git merge upstream/main` einen Konflikt erzeugen würde. Siehe `CLAUDE.md`.

## Benötigte Repository-Secrets

Zu setzen unter *Settings → Secrets and variables → Actions*, oder per CLI:

```bash
gh secret set JELASTIC_TOKEN    --repo pti-schweiz/pti-pseudo
gh secret set JELASTIC_SSH_KEY  --repo pti-schweiz/pti-pseudo < ~/.ssh/jelastic_ci
gh secret set JELASTIC_SSH_USER --repo pti-schweiz/pti-pseudo --body 10792
```

| Secret | Bedeutung | Herkunft |
|---|---|---|
| `JELASTIC_TOKEN` | Jelastic-API-Token | jCloud-Dashboard → Settings → Access Tokens. Braucht die Methoden `environment.Vcs.Update` **und** `environment.Control.RestartNodes`. Fehlt eine, kommt `result: 8202` mit „Token is not allowed for method". |
| `JELASTIC_SSH_KEY` | privater SSH-Schlüssel (PEM) | `~/.ssh/jelastic_ci` — kontoweit hinterlegt, wird auch von den Schwestertools genutzt. Der öffentliche Teil liegt in den Jelastic-Account-Settings unter SSH Keys. |
| `JELASTIC_SSH_USER` | SSH-Benutzer am Gateway | Die numerische Jelastic-Konto-ID, bei PTI `10792`. Nicht der Node-Name. |

## Einmalige Voraussetzungen im jCloud-Panel

Nur noch zwei, und beide haben nichts mit GitHub zu tun:

1. **Umgebungsvariablen** setzen (Liste in `.env.example`), danach
   **Dashboard-Restart** — ein Prozess-Restart uebernimmt sie nicht.
2. **PostgreSQL-Datenbank** anlegen und die `DATABASE_URL` eintragen.

**Kein VCS-Dialog, kein GitHub-Token auf dem Server.** Die Deployment-Funktion
des Panels ist bei „Python Engine" ausgegraut — sie gehoert zu den verwalteten
Anwendungsservern. Der Code kommt per rsync ueber den SSH-Gate, siehe
`JELASTIC_SETUP.md` Abschnitt 0.

## Was dieser Workflow anders macht als die Schwestertools

Zweierlei, beides erzwungen durch den Stack:

**Kein VCS-Update.** Der Knotentyp kann es nicht (`vcs/rest/update` antwortet
`result 2500`). Statt Panel-Pull laeuft ein `rsync --delete` des gesamten
Repositories nach `/home/jelastic/ROOT`. `.git` ist ausgeschlossen — die
Presidio-Historie ist rund 340 MB.

**Kein npm auf dem Knoten.** Das Frontend wird im Runner gebaut und geht beim
selben rsync mit.

**Konsequenz:** Es gibt keinen Panel-Knopf mehr, der einen Deploy ausloest. Der
einzige Weg ist diese Action — oder `scripts/deploy-prod.ps1` von Hand.

## Manueller Rückfall

`scripts/deploy-prod.ps1` macht dasselbe von Hand. Es braucht die gitignorierten
Dateien `.claude/jelastic-token` und `.claude/jelastic.config.json`
(Vorlage: `.claude/jelastic.config.example.json`).

Wichtig: Das Skript geht für den Build-Schritt über den SSH-Gate und **nicht**
über die Jelastic-`execcmd`-API. Diese Methode steht nicht in der Token-Whitelist
und antwortet mit `result 8202`.

## Offener Verbesserungsvorschlag: paths-ignore

Der Workflow reagiert auf **jeden** Push nach `main` — auch auf reine Test-,
Doku- oder Golden-Set-Änderungen, die in Produktion nichts bewirken. Jeder
solche Lauf kostet ein Ausfallfenster von drei bis sechs Minuten.

```yaml
on:
  push:
    branches: [main]
    paths-ignore:
      - 'golden-set/**'
      - 'backend/tests/**'
      - 'docs/**'
      - '**.md'
```

Das ist nicht Kosmetik: je öfter ein Deploy ohne Not läuft, desto eher gewöhnt
man sich an Ausfallfenster — und übersieht dann eines, das etwas bedeutet.

Noch nicht umgesetzt, weil es das Deploy-Verhalten ändert und beim Nutzer
bestätigt werden sollte.
