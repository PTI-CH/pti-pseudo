# Manueller Produktiv-Deploy als Rückfall, wenn die GitHub-Action nicht läuft.
#
# WICHTIG: Diese Datei muss als UTF-8 MIT BOM gespeichert werden. Windows
# PowerShell 5.1 interpretiert eine BOM-lose UTF-8-Datei als ANSI und zerlegt
# damit jeden Umlaut und jedes Sonderzeichen in diesen Kommentaren.
#
# Der Build-Schritt läuft bewusst über den SSH-Gate und NICHT über die
# Jelastic-Methode control/rest/execcmd: die steht nicht in der Whitelist des
# Deploy-Tokens und antwortet mit result 8202.
#
# Voraussetzungen:
#   .claude/jelastic-token        (Klartext-Token, gitignored)
#   .claude/jelastic.config.json  (Vorlage: jelastic.config.example.json)
#   ~/.ssh/jelastic_ci            (privater Schlüssel, kontoweit hinterlegt)
#   Node.js + npm lokal, für den Frontend-Build
#
# Aufruf:
#   .\scripts\deploy-prod.ps1
#   .\scripts\deploy-prod.ps1 -SkipPush          # nur deployen, nicht pushen
#   .\scripts\deploy-prod.ps1 -SkipFrontend      # nur Backend-Abhängigkeiten

[CmdletBinding()]
param(
    [switch]$SkipPush,
    [switch]$SkipFrontend,
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

# ── Konfiguration einlesen ─────────────────────────────────────────────────
$tokenPath  = Join-Path $repoRoot ".claude\jelastic-token"
$configPath = Join-Path $repoRoot ".claude\jelastic.config.json"

if (-not (Test-Path $tokenPath)) {
    throw "Token fehlt: $tokenPath — im jCloud-Dashboard unter Settings > Access Tokens erzeugen."
}
if (-not (Test-Path $configPath)) {
    throw "Konfiguration fehlt: $configPath — Vorlage ist .claude\jelastic.config.example.json."
}

$token  = (Get-Content $tokenPath -Raw).Trim()
$config = Get-Content $configPath -Raw | ConvertFrom-Json

if ($config.appNodeId -eq 0) {
    throw "appNodeId in $configPath ist noch 0 — Node-ID des Anwendungsservers aus dem jCloud-Panel eintragen."
}

$apiBase  = "https://$($config.hosterApi)/1.0/environment"
$sshUser  = "10792"
$sshGate  = "gate.jpc.infomaniak.com"
$sshPort  = "3022"
$sshKey   = Join-Path $env:USERPROFILE ".ssh\jelastic_ci"
$rootDir  = "/home/jelastic/ROOT"
$venvDir  = "/home/jelastic/venv"

function Invoke-JelasticApi {
    param([string]$Path, [hashtable]$Body)
    $Body.session = $token
    $Body.envName = $config.envName
    $response = Invoke-RestMethod -Uri "$apiBase/$Path" -Method Post -Body $Body `
        -ContentType "application/x-www-form-urlencoded"
    if ($response.result -ne 0) {
        throw "Jelastic-API $Path fehlgeschlagen: result=$($response.result) $($response.error)"
    }
    return $response
}

function Invoke-NodeSsh {
    param([string]$Command)
    if (-not (Test-Path $sshKey)) { throw "SSH-Schlüssel fehlt: $sshKey" }
    & ssh -i $sshKey -p $sshPort -o StrictHostKeyChecking=accept-new `
        "$sshUser@$sshGate" $Command
    if ($LASTEXITCODE -ne 0) { throw "SSH-Befehl fehlgeschlagen (Exit $LASTEXITCODE): $Command" }
}

Write-Host "=== Deploy $($config.envName) → $($config.liveUrl) ===" -ForegroundColor Cyan

# ── 0/5 Push ───────────────────────────────────────────────────────────────
if (-not $SkipPush) {
    Write-Host "`n[0/5] git push origin $Branch" -ForegroundColor Yellow
    & git -C $repoRoot push origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "git push fehlgeschlagen" }
} else {
    Write-Host "`n[0/5] Push übersprungen (-SkipPush)" -ForegroundColor DarkGray
}

# ── 1/5 Frontend bauen ─────────────────────────────────────────────────────
if (-not $SkipFrontend) {
    Write-Host "`n[1/5] Frontend bauen" -ForegroundColor Yellow
    Push-Location (Join-Path $repoRoot "frontend")
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci fehlgeschlagen" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build fehlgeschlagen" }
    } finally { Pop-Location }
} else {
    Write-Host "`n[1/5] Frontend-Build übersprungen (-SkipFrontend)" -ForegroundColor DarkGray
}

# ── 2/5 Code holen ─────────────────────────────────────────────────────────
Write-Host "`n[2/5] Jelastic VCS-Update" -ForegroundColor Yellow
Invoke-JelasticApi -Path "vcs/rest/update" -Body @{ context = "ROOT" } | Out-Null
Write-Host "  ok" -ForegroundColor Green

# ── 3/5 Frontend hochladen ─────────────────────────────────────────────────
# Nach dem VCS-Update, weil dieses den Inhalt von ROOT ersetzt.
if (-not $SkipFrontend) {
    Write-Host "`n[3/5] frontend/dist hochladen" -ForegroundColor Yellow
    $distPath = Join-Path $repoRoot "frontend\dist"
    if (-not (Test-Path (Join-Path $distPath "index.html"))) {
        throw "frontend/dist/index.html fehlt — Build zuerst laufen lassen."
    }
    Invoke-NodeSsh "mkdir -p $rootDir/frontend/dist"
    & scp -i $sshKey -P $sshPort -o StrictHostKeyChecking=accept-new -r `
        "$distPath\*" "$sshUser@${sshGate}:$rootDir/frontend/dist/"
    if ($LASTEXITCODE -ne 0) { throw "scp fehlgeschlagen" }
    Write-Host "  ok" -ForegroundColor Green
}

# ── 4/5 Abhängigkeiten ─────────────────────────────────────────────────────
Write-Host "`n[4/5] Abhängigkeiten und Modell" -ForegroundColor Yellow
$depCmd = @"
set -e
[ -d $venvDir ] || python3 -m venv $venvDir
$venvDir/bin/pip install --upgrade pip
$venvDir/bin/pip install -r $rootDir/pti-requirements.txt
$venvDir/bin/python -c 'import de_core_news_md; print("spaCy-Modell OK")'
$venvDir/bin/python -c 'import presidio_analyzer, presidio_anonymizer; print("Presidio OK")'
chmod +x $rootDir/deploy/run.sh
"@
Invoke-NodeSsh $depCmd
Write-Host "  ok" -ForegroundColor Green

# ── 5/5 Neustart ───────────────────────────────────────────────────────────
Write-Host "`n[5/5] Knoten neu starten (nodeId=$($config.appNodeId))" -ForegroundColor Yellow
Invoke-JelasticApi -Path "control/rest/restartnodes" -Body @{ nodeId = $config.appNodeId } | Out-Null
Write-Host "  ok" -ForegroundColor Green

# ── Smoke-Test ─────────────────────────────────────────────────────────────
# Geduld ist Absicht: ein Jelastic-Restart hat typisch ZWEI Ausfallfenster mit
# einer trügerischen 200 dazwischen. Nicht beim ersten Erfolg aufhören.
Write-Host "`n[Smoke] Warte 30s auf gunicorn und Modell-Ladezeit..." -ForegroundColor Yellow
Start-Sleep -Seconds 30

$healthOk = $false
for ($i = 1; $i -le 12; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "$($config.liveUrl)/api/health" -TimeoutSec 10
        if ($r.ok -eq $true) {
            Write-Host "  Health OK ($i. Versuch)" -ForegroundColor Green
            $healthOk = $true
            break
        }
    } catch {
        Write-Host "  Versuch $i fehlgeschlagen, retry in 10s..." -ForegroundColor DarkGray
    }
    Start-Sleep -Seconds 10
}
if (-not $healthOk) {
    throw "Health-Check schlug 12x fehl. Auf dem Knoten pruefen: tail -n 100 /var/log/pythonengine.log"
}

# Zweiter Check: /api/health ist SSO-ausgenommen und damit blind fuer einen
# Fehler, der alle anderen Routen sperrt. Eine gegatete Route muss ohne Session
# mit 401 oder 302 antworten.
try {
    $resp = Invoke-WebRequest -Uri "$($config.liveUrl)/api/v1/terms" -TimeoutSec 10 `
        -MaximumRedirection 0 -ErrorAction Stop
    $code = $resp.StatusCode
} catch {
    $code = $_.Exception.Response.StatusCode.value__
}
switch ($code) {
    401 { Write-Host "  SSO-Gate antwortet erwartungsgemaess (401)" -ForegroundColor Green }
    302 { Write-Host "  SSO-Gate antwortet erwartungsgemaess (302)" -ForegroundColor Green }
    200 { Write-Warning "  Gegatete Route ist ohne Login offen (200) — steht HUB_AUTH=off in der Prod-Env?" }
    default { throw "Gegatete Route liefert $code — App/SSO-Gate defekt." }
}

Write-Host "`n=== Deploy abgeschlossen: $($config.liveUrl) ===" -ForegroundColor Cyan
