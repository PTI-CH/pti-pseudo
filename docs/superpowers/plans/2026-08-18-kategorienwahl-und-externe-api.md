# Kategorienwahl und externe API — Umsetzungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kategorienwahl (Entitätstypen) in GUI und API, plus eine schlüsselgeschützte externe API unter `/api/ext/v1/` mit Admin-Verwaltung der Schlüssel.

**Architecture:** Die bestehenden Router (`documents`, `vaults`, `terms`, neu `entities`) verlieren ihr fest verdrahtetes Präfix und werden zweimal eingehängt: unter `/api/v1` (SSO-Gate) und unter `/api/ext/v1` (Router-Dependency `require_api_key`, vom SSO-Gate ausgenommen). Ein einheitlicher Aktor-Begriff in `backend/core/deps.py` macht Hub-User und API-Schlüssel für Handler ununterscheidbar. Schlüssel liegen gehasht in einer neuen Tabelle `api_keys`.

**Tech Stack:** FastAPI, SQLAlchemy 2, pydantic, pytest/TestClient (SQLite in-memory), React 18 + TypeScript + Tailwind, Vite.

**Spec:** `docs/superpowers/specs/2026-08-18-kategorienwahl-und-externe-api-design.md`

## Global Constraints

- UI-Texte, Code-Kommentare, Docstrings, Commit-Messages **auf Deutsch**; Bezeichner englisch. Conventional Commits mit Scope.
- Kein PTI-Inhalt in Upstream-Dateien (`presidio-*/`, `docs/` ausser `docs/superpowers/`, `pyproject.toml`, `.gitignore`).
- Keine echten Personendaten in Tests. Echte Umlaute in Testtexten.
- Tests: `.venv/Scripts/python -m pytest backend/tests -q` (Windows). Golden-Set-Gate muss grün bleiben.
- Frontend: `cd frontend && npm run build` muss sauber durchlaufen (tsc + vite).
- Kein Schema-Umbau bestehender Tabellen — nur die neue Tabelle `api_keys`.
- Token nie loggen, nie in Antworten ausser bei der Erzeugung.

---

### Task 1: `entities`-Filter in Pipeline und Endpunkten, Katalog-Endpunkt

**Files:**
- Modify: `backend/pipeline.py` (`_select`, `anonymize_text`, `anonymize_document`)
- Modify: `backend/routers/documents.py` (`TextRequest.entities`, `detect`, `anonymize`, `anonymize_file`)
- Create: `backend/routers/entities.py`
- Modify: `backend/main.py` (Router einhängen)
- Test: `backend/tests/test_entities_filter.py`

**Interfaces:**
- Produces: `pipeline._select(findings, selected, *, entities=None)` — bei `entities` gesetzt und `selected is None` werden alle Funde zurückgegeben, deren Typ in `entities` liegt (KEEP-Policy ignoriert). `pipeline.anonymize_text(..., entities=...)` und `anonymize_document(..., entities=...)` reichen `entities` an `_select` weiter (an `detect_in_segments` bereits heute).
- Produces: `documents.parse_entities(value: list[str] | str | None) -> list[str] | None` — validiert gegen `ents.ALL_ENTITIES`, wirft `HTTPException(400)` bei unbekanntem Typ oder leerer Liste; String wird an Komma getrennt und getrimmt.
- Produces: `GET /api/v1/entities` → `{"entities": [{"entity_type","label","description","default_policy","source"}]}` mit `source = "custom_terms"` für `ents.NUR_BEGRIFFSLISTE`, sonst `"recognizer"`.

- [ ] **Step 1: Failing Tests schreiben** (`backend/tests/test_entities_filter.py`; Fixture wie in `test_integration_api.py` — dieselbe MonkeyPatch-Konstruktion kopieren, nicht importieren)

```python
TEXT = "Frau Petra Müller (IBAN CH93 0076 2011 6238 5295 7, Tel. 044 123 45 67) wohnt in 8001 Zürich."

def test_katalog_endpunkt_liefert_alle_typen(client):
    r = client.get("/api/v1/entities")
    assert r.status_code == 200
    typen = {e["entity_type"]: e for e in r.json()["entities"]}
    assert "PERSON" in typen and "CH_IBAN" in typen and "PTI_PROJEKT" in typen
    assert typen["CH_POSTAL_CODE"]["default_policy"] == "keep"
    assert typen["PTI_PROJEKT"]["source"] == "custom_terms"
    assert typen["PERSON"]["label"] == "Person"

def test_detect_mit_entities_liefert_nur_gewaehlte_typen(client):
    r = client.post("/api/v1/documents:detect", json={"text": TEXT, "entities": ["CH_IBAN"]})
    assert r.status_code == 200, r.text
    typen = {e["entity_type"] for e in r.json()["entities"]}
    assert typen == {"CH_IBAN"}, typen

def test_anonymize_mit_entities_ersetzt_nur_gewaehlte_typen(client):
    r = client.post("/api/v1/documents:anonymize",
                    json={"text": TEXT, "mode": "anonymous", "entities": ["CH_IBAN"]})
    assert r.status_code == 200, r.text
    assert "Petra Müller" in r.json()["text"]          # Name bleibt
    assert "CH93" not in r.json()["text"]               # IBAN ersetzt

def test_explizite_wahl_ersetzt_auch_keep_typen(client):
    r = client.post("/api/v1/documents:anonymize",
                    json={"text": "Sie wohnt an der Seestrasse 5, 8001 Zürich.",
                          "mode": "anonymous", "entities": ["CH_POSTAL_CODE"]})
    assert r.status_code == 200, r.text
    assert "8001" not in r.json()["text"]

def test_ohne_entities_bleiben_keep_typen_stehen(client):
    r = client.post("/api/v1/documents:anonymize",
                    json={"text": "Sie wohnt an der Seestrasse 5, 8001 Zürich.", "mode": "anonymous"})
    assert "8001" in r.json()["text"]

def test_unbekannter_typ_gibt_400(client):
    r = client.post("/api/v1/documents:detect", json={"text": TEXT, "entities": ["FOO"]})
    assert r.status_code == 400
    assert "FOO" in r.json()["detail"]

def test_leere_liste_gibt_400(client):
    r = client.post("/api/v1/documents:detect", json={"text": TEXT, "entities": []})
    assert r.status_code == 400

def test_datei_mit_entities_als_kommaliste(client):
    files = {"file": ("t.txt", TEXT.encode("utf-8"), "text/plain")}
    r = client.post("/api/v1/documents:anonymize-file", files=files,
                    data={"mode": "anonymous", "entities": "CH_IBAN, CH_PHONE"})
    assert r.status_code == 200, r.text
    out = base64.b64decode(r.json()["content_base64"]).decode("utf-8")
    assert "Petra Müller" in out and "CH93" not in out
```

- [ ] **Step 2: Laufen lassen, Fehlschlag bestätigen** — `.venv/Scripts/python -m pytest backend/tests/test_entities_filter.py -q`
- [ ] **Step 3: `pipeline._select` erweitern**

```python
def _select(findings, selected, *, entities=None):
    if selected is not None:
        wanted = {(s.get("segment_id"), s.get("start"), s.get("end")) for s in selected}
        return [f for f in findings if (f.segment_id, f.start, f.end) in wanted]
    if entities is not None:
        # Ausdrueckliche Wahl schlaegt die Default-Policy: wer die PLZ anhakt,
        # will sie ersetzt haben — nicht nur markiert.
        erlaubt = set(entities)
        return [f for f in findings if f.entity_type in erlaubt]
    return [f for f in findings if ents.get_policy(f.entity_type) is not ents.Policy.KEEP]
```
`anonymize_text`/`anonymize_document`: `_select(detection.findings, selected, entities=entities)`.

- [ ] **Step 4: Router** — `TextRequest.entities: Optional[list[str]] = Field(None, description=...)`; `anonymize_file` bekommt `entities: Annotated[Optional[str], Form()] = None`. `parse_entities` in `documents.py`; alle drei Handler rufen `entities = parse_entities(payload.entities)` und geben `entities=entities` an `pipeline.detect_in_segments`/`anonymize_text`/`anonymize_document` weiter. Neuer Router `backend/routers/entities.py` (`APIRouter(prefix="/api/v1", tags=["entities"])`, Prefix wird in Task 2 entfernt) mit `GET /entities`. In `main.py` einhängen.
- [ ] **Step 5: Tests grün** — gesamte Suite `.venv/Scripts/python -m pytest backend/tests -q`
- [ ] **Step 6: Commit** — `feat(api): Kategorienwahl per entities-Filter und Katalog-Endpunkt`

---

### Task 2: API-Schlüssel — Modell, Authentisierung, zweiter Einhängepunkt `/api/ext/v1`

**Files:**
- Modify: `backend/vault/models.py` (neue Klasse `ApiKey`)
- Create: `backend/core/api_key_auth.py`
- Modify: `backend/core/deps.py` (`get_tenant(session, request=None)`, `current_username`, neu `current_actor`)
- Modify: `backend/routers/documents.py`, `vaults.py`, `terms.py`, `entities.py` (Präfix entfernen → `APIRouter(tags=[...])`; alle `get_tenant(session)` → `get_tenant(session, request)`; Handler ohne `request`-Parameter bekommen einen)
- Modify: `backend/main.py` (zweimal einhängen; `EXEMPT_PATHS` um `^/api/ext/` ergänzen)
- Modify: `backend/routers/vaults.py`, `terms.py`: `is_admin(request)`-Gates → `is_admin(request) or has_api_key(request)`
- Test: `backend/tests/test_api_key_auth.py`

**Interfaces:**
- Produces (`models.py`):
```python
class ApiKey(Base):
    __tablename__ = "api_keys"
    id: uuid PK; tenant_id: FK tenants.id (ondelete CASCADE); label: String(200)
    key_prefix: String(16); key_hash: String(64) unique index
    created_by: String(150); created_at: DateTime(timezone=True) default utcnow
    expires_at, last_used_at, revoked_at: DateTime(timezone=True) nullable
    @property def is_active(self) -> bool  # nicht widerrufen und nicht abgelaufen
```
- Produces (`api_key_auth.py`):
```python
TOKEN_PREFIX = "ptps_"
def generate_token() -> str                      # TOKEN_PREFIX + secrets.token_urlsafe(32)
def hash_token(token: str) -> str                # sha256 hex
def key_prefix_of(token: str) -> str             # token[:12]
def read_api_key_header(request) -> str | None   # X-Api-Key, sonst Authorization: Bearer
def has_api_key(request) -> bool                 # request.state.api_key gesetzt
async def require_api_key(request: Request, session: Session = Depends(get_session)) -> ApiKey
    # 401 {"code":"API_KEY_MISSING"} / {"code":"API_KEY_INVALID"} als HTTPException(detail=...)
    # HTTPException(status_code=401, detail={"code": ..., "message": ...})
    # setzt request.state.api_key; aktualisiert last_used_at, wenn > 60 s alt oder None
LAST_USED_WRITE_INTERVAL_SECONDS = 60
```
- Produces (`deps.py`):
```python
def get_tenant(session: Session, request: Request | None = None) -> Tenant
    # request.state.api_key vorhanden -> dessen Tenant; sonst Default-Mandant
def current_username(request) -> str   # Hub-Username, oder f"apikey:{api_key.label}"
```
- Produces (`main.py`): `for prefix, deps in (("/api/v1", []), ("/api/ext/v1", [Depends(require_api_key)])): app.include_router(r, prefix=prefix, dependencies=deps)` für `documents`, `vaults`, `terms`, `entities`. Der ext-Einhängepunkt bekommt `tags`-Präfix nicht — die Tags bleiben, damit `/api/docs` beide zeigt.

- [ ] **Step 1: Failing Tests** (`backend/tests/test_api_key_auth.py`; Fixture: eigene App mit `HUB_AUTH=off`, SQLite; ein Hilfs-Fixture legt einen Schlüssel **direkt über das Modell** an — der Admin-Endpunkt kommt erst in Task 3):

```python
def _lege_schluessel_an(label="Testapp", expires_at=None, revoked_at=None) -> str:
    from backend.core.api_key_auth import generate_token, hash_token, key_prefix_of
    from backend.core.deps import get_tenant
    from backend.db import get_sessionmaker
    from backend.vault.models import ApiKey
    token = generate_token()
    with get_sessionmaker()() as s:
        tenant = get_tenant(s)
        s.add(ApiKey(tenant_id=tenant.id, label=label, key_prefix=key_prefix_of(token),
                     key_hash=hash_token(token), created_by="test",
                     expires_at=expires_at, revoked_at=revoked_at))
        s.commit()
    return token

def test_token_hat_praefix_und_laenge():
    t = generate_token(); assert t.startswith("ptps_") and len(t) >= 40

def test_ohne_header_401_mit_code(client):
    r = client.post("/api/ext/v1/documents:detect", json={"text": "Hallo"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "API_KEY_MISSING"

def test_falscher_token_401(client):
    r = client.post("/api/ext/v1/documents:detect", json={"text": "Hallo"}, headers={"X-Api-Key": "ptps_falsch"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "API_KEY_INVALID"

def test_gueltiger_token_per_x_api_key_und_bearer(client):
    t = _lege_schluessel_an()
    for h in ({"X-Api-Key": t}, {"Authorization": f"Bearer {t}"}):
        r = client.post("/api/ext/v1/documents:detect", json={"text": "Herr Andreas Meier"}, headers=h)
        assert r.status_code == 200, r.text

def test_widerrufener_und_abgelaufener_token_401(client): ...   # revoked_at=now bzw. expires_at=now-1h

def test_rundlauf_anonymize_reverse_ueber_ext(client): ...     # Text und Datei, wie test_integration_api

def test_nutzungseintrag_traegt_apikey_aktor(client):
    # nach einem ext-Aufruf: UsageEvent.username == "apikey:Testapp"

def test_ext_pfad_verlangt_schluessel_auch_wenn_hub_auth_off(client):
    # identisch zu test_ohne_header_401 — dokumentiert die Absicht

def test_v1_pfad_bleibt_hinter_sso_gate():
    # eigene App mit HUB_AUTH="on"-Settings und fetch_impl, das {"authenticated": False} liefert:
    # /api/v1/documents:detect -> 401 UNAUTHENTICATED; /api/ext/v1/... ohne Key -> 401 API_KEY_MISSING (kein Redirect)
```

- [ ] **Step 2: Fehlschlag bestätigen**
- [ ] **Step 3: Modell `ApiKey`** in `models.py` (Docstring: warum Hash, warum Soft-Delete). `init_db` legt die Tabelle automatisch an (`create_all`).
- [ ] **Step 4: `api_key_auth.py`** — Lookup: `select(ApiKey).where(ApiKey.key_hash == hash_token(token))`; zusätzlich `hmac.compare_digest(row.key_hash, hash_token(token))`; `is_active` prüfen; `last_used_at` nur schreiben, wenn `None` oder älter als 60 s (`session.commit()`), Fehler beim Schreiben nur loggen, nie den Request scheitern lassen.
- [ ] **Step 5: `deps.py`** anpassen, alle Aufrufer von `get_tenant` in den vier Routern auf `(session, request)`. `is_admin(request) or has_api_key(request)` in `vaults.remove_vault` und den Admin-Gates in `terms.py` (Docstring: API-Schlüssel gilt innerhalb seines Mandanten als vollberechtigt, siehe Spec §4.4).
- [ ] **Step 6: `main.py`** — Präfixe aus Routern entfernen, doppelt einhängen, `EXEMPT_PATHS += re.compile(r"^/api/ext/")`. `me.router` und `health.router` unverändert.
- [ ] **Step 7: Gesamte Suite grün** — insbesondere `test_integration_api.py` (Pfadänderung darf nichts brechen).
- [ ] **Step 8: Commit** — `feat(api): externe API unter /api/ext/v1 mit API-Schluesseln`

---

### Task 3: Schlüsselverwaltung — Admin-Endpunkte

**Files:**
- Create: `backend/routers/api_keys.py`
- Modify: `backend/main.py` (nur unter `/api/v1` einhängen — **nicht** unter ext)
- Test: `backend/tests/test_api_keys_admin.py`

**Interfaces:**
```
GET    /api/v1/api-keys      -> {"api_keys": [{"id","label","key_prefix","created_by","created_at","expires_at","last_used_at","revoked_at","active": bool}]}  neueste zuerst
POST   /api/v1/api-keys      body {"label": str(1..200), "expires_at": datetime|null} -> 201 {…wie oben…, "token": "<klartext>"}
DELETE /api/v1/api-keys/{id} -> {"revoked": true}; 404 wenn fremder Mandant/unbekannt; idempotent (zweiter Aufruf 200)
```
Alle drei: `is_admin(request)` sonst 403 `{"detail": "Nur Administratoren duerfen API-Schluessel verwalten."}`. Nutzt `generate_token/hash_token/key_prefix_of` aus Task 2.

- [ ] **Step 1: Failing Tests** — Lebenszyklus: POST → Antwort enthält `token` und `key_prefix == token[:12]`; GET zeigt kein `token`/`key_hash`; Aufruf `/api/ext/v1/documents:detect` mit dem Token → 200; DELETE → `/api/ext/v1/...` → 401 `API_KEY_INVALID`; GET zeigt `active: false`; `DEV_ROLE=standard`-App: POST → 403; `/api/ext/v1/api-keys` → 404 (nicht eingehängt).
- [ ] **Step 2: Fehlschlag bestätigen**
- [ ] **Step 3: Router schreiben** (Docstring: warum die Verwaltung nur über SSO erreichbar ist)
- [ ] **Step 4: Suite grün**
- [ ] **Step 5: Commit** — `feat(api-keys): Admin-Endpunkte zum Erzeugen und Widerrufen von API-Schluesseln`

---

### Task 4: Frontend — API-Client (Dateipfad-Fix, `entities`, Katalog, Schlüssel) und Kategorienwahl im Panel

**Files:**
- Modify: `frontend/src/core/api.ts`
- Create: `frontend/src/components/KategorienWahl.tsx`
- Modify: `frontend/src/panels/Pseudonymisieren.tsx`
- Modify: `frontend/src/core/entities.ts` (nur Kommentar: Labels kommen jetzt vom Server; `labelFuerTyp` bleibt als Fallback)

**Interfaces (api.ts):**
```ts
export interface Kategorie { entity_type: string; label: string; description: string; default_policy: 'redact' | 'keep'; source: 'recognizer' | 'custom_terms'; }
export async function ladeKategorien(): Promise<Kategorie[]>            // GET /api/v1/entities
export async function anonymisiereText(text, modus, kategorien: string[], nurBefunde?, signal?)  // sendet entities
export async function anonymisiereDatei(datei, modus, kategorien: string[], signal?)             // POST /api/v1/documents:anonymize-file, FormData entities=kommaliste
export async function rueckfuehrenDatei(datei, vaultId, signal?)                                  // POST /api/v1/documents:reverse-file
// content_base64 -> Blob-URL: downloadUrl = URL.createObjectURL(new Blob([bytes])), dateiname = filename
export interface ApiSchluessel { id; label; keyPrefix; createdBy; createdAt; expiresAt; lastUsedAt; revokedAt; aktiv: boolean }
export async function ladeApiSchluessel(): Promise<ApiSchluessel[]>
export async function erzeugeApiSchluessel(label: string, expiresAt: string | null): Promise<{ schluessel: ApiSchluessel; token: string }>
export async function widerrufeApiSchluessel(id: string): Promise<void>
```
**KategorienWahl-Props:** `{ kategorien: Kategorie[]; gewaehlt: Set<string>; onGewaehlt: (s: Set<string>) => void; deaktiviert?: boolean }` — Checkbox-Grid (2–3 Spalten), Label + `title`-Tooltip mit description, KEEP-Typen mit Zusatz „(standardmässig nur markiert)", Knöpfe „Alle" / „Keine", Zähler „n von m gewählt". Panel: lädt Katalog beim Mount, Vorbelegung = alle mit `default_policy === 'redact'`; Start-Knopf deaktiviert bei leerer Auswahl mit Hinweis.

- [ ] **Step 1: api.ts umbauen** (inkl. Blob-URL-Freigabe: alte URL per `URL.revokeObjectURL` beim Zurücksetzen — im Panel)
- [ ] **Step 2: KategorienWahl.tsx** und Einbau ins Panel zwischen Eingabe und Modus
- [ ] **Step 3: `cd frontend && npm run build`** — muss ohne Fehler durchlaufen
- [ ] **Step 4: Commit** — `feat(gui): Kategorienwahl vor dem Lauf; Dateipfad auf die richtigen Routen`

---

### Task 5: Frontend — Bereich „API-Schlüssel" (nur Admin)

**Files:**
- Create: `frontend/src/panels/ApiSchluessel.tsx`
- Modify: `frontend/src/App.tsx` (vierter Bereich, nur wenn `useBenutzer().isAdmin`; Icon `KeyRound` ist schon in Verwendung — nimm `Plug` oder `Webhook` aus lucide)
- Modify: `frontend/src/components/Kopfzeile.tsx` nur falls `useBenutzer` dort exklusiv aufgerufen wird und ein zweiter Aufruf doppelte Requests erzeugt → dann `benutzer` als Prop aus App durchreichen.

- [ ] **Step 1: Panel** — Tabelle (Bezeichnung, Präfix `ptps_xxxxxxx…`, erstellt von/am, zuletzt benutzt, Status-Badge aktiv/widerrufen/abgelaufen), Formular (Bezeichnung Pflicht, Ablaufdatum optional `<input type="date">` → ISO), nach Erzeugen ein `Hinweis ton="warnung"` mit Token in `<code>`, Kopieren-Knopf (Muster aus `ErgebnisFeld`), Text „Dieser Schlüssel wird nur jetzt angezeigt. Notieren Sie ihn in der aufrufenden Anwendung."; „Widerrufen" mit `window.confirm`. Doku-Kasten (`Hinweis ton="info"`) mit cURL-Beispiel und Verweis auf `API.md`.
- [ ] **Step 2: App.tsx** — Bereich nur rendern, wenn Admin.
- [ ] **Step 3: `npm run build`** grün
- [ ] **Step 4: Commit** — `feat(gui): Verwaltung der API-Schluessel fuer Administratoren`

---

### Task 6: Dokumentation und Abschluss

**Files:**
- Create: `API.md` (Repo-Root)
- Modify: `README.MD` (Abschnitt „Externe API" mit Verweis), `CLAUDE.md` (Architektur-Absatz: zwei Einhängepunkte, wo Schlüssel liegen; Falle: `/api/ext/` ist SSO-exempt, darf nie ohne `require_api_key` eingehängt werden)
- Modify: `.env.example` — nichts nötig (keine neue Env-Variable); prüfen.

- [ ] **Step 1: `API.md`** — Abschnitte: Zugang (Schlüssel im Tool erzeugen; Header; nur HTTPS), Endpunkte mit je einem cURL-Beispiel und Antwort (`detect`, `anonymize` Text mit `entities` und `mode`, `anonymize-file` Base64, `reverse`, `reverse-file`, `mappings`, `terms`, `entities`), Fehlerformat (401 Codes, 400/404/413/415), Grenzen (25 MiB, 300'000 Zeichen), Hinweis Vault-ID aufbewahren.
- [ ] **Step 2: README + CLAUDE.md**
- [ ] **Step 3: Vollständiger Lauf** — `.venv/Scripts/python -m pytest backend/tests -q` und `cd frontend && npm run build`
- [ ] **Step 4: Commit** — `docs: externe API dokumentiert (API.md), Hinweise in README und CLAUDE.md`
