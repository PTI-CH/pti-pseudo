# Design: KI-Schicht für Personennamen (Phase 3, Variante C)

Datum: 2026-08-24 · Status: entworfen, nicht umgesetzt · Setzt Phase 3 des
Fachkonzepts (§7 Variante C, §10) um — den Teil davon, der den gemessenen
Fehler behebt.

## 1. Das Problem, gemessen

Der Golden-Set-Lauf vom 2026-08-24 verfehlt bei Personennamen **einen einzigen**
erwarteten Fund: `Sandra Zbinden` in der Fusszeile von
`tabelle_kopf_fusszeile`. Er ist nicht übersehen — spaCy findet `Sandra`, aber
die Grenze sitzt falsch. Das kostet doppelt: ein verfehlter Fund plus ein
Fehlalarm auf dem Vornamen.

Dieser **Namenssplit** ist der gefährlichste Fehler des ganzen Werkzeugs. Der
Nachname bleibt im Dokument stehen, und das Ergebnis *sieht aus* wie ein
pseudonymisiertes Dokument. Es gibt nichts, woran jemand den Fehler bemerken
könnte.

Gegen ihn hilft weder ein Muster noch eine Prüfziffer noch die Wortart-Regel
aus `engine/wortarten.py` — nur ein Modell, das den umgebenden Satz liest.

Qualitätsziel des Fachkonzepts: **Namen-Recall ≥ 95 %.** Heute 95.0 %, aber mit
genau diesem Fehlerbild.

## 2. Vier Entscheidungen, die das Design tragen

Alle vier wurden am 2026-08-24 mit dem Auftraggeber getroffen. Sie stehen hier
mit Begründung, weil jede von ihnen später anders ausfallen kann und dann
nachvollziehbar sein muss, was sie gekostet hat.

### 2.1 Was das Haus verlässt: **nur Fenster um Fundkandidaten**

Pro fraglicher Stelle gehen rund 15 Wörter hinaus, nicht das Segment und nicht
das Dokument. Abschnitt 8 hält die Ausbaustufen fest.

**Was das ausschliesst:** Ein Name, den spaCy *vollständig* übersieht, bleibt
übersehen — die KI sieht nur Stellen, an denen die lokale Erkennung schon
etwas gefunden hat.

**Warum es trotzdem trägt:** Der einzige gemessene Fehlschlag ist ein
Grenzfehler, kein Totalausfall. Und `pipeline.propagate_findings` läuft
**nach** der KI: Wird `Sandra` zu `Sandra Zbinden` korrigiert, trägt die
Übertragung den vollständigen Namen an alle weiteren Stellen im Dokument —
auch dorthin, wo spaCy ihn übersehen hat. Die im Fachkonzept versprochene
„Zusammenfassung gleicher Personen über das ganze Dokument" entsteht für den
häufigen Fall aus dieser Kombination.

**Das Fachkonzept verspricht an dieser Stelle mehr, als Variante A liefert.**
Der Text gehört angepasst, siehe Abschnitt 9.

### 2.2 Bei Ausfall: **weiterlaufen mit sichtbarem Hinweis**

Der Dienst wird ausfallen. Dann liefert der Lauf das Ergebnis der lokalen
Erkennung, und die Oberfläche sagt es deutlich.

**Warum nicht scheitern lassen:** Die lokale Erkennung ist der heutige
Normalbetrieb mit 95 % Namen-Recall — kein Notbehelf. Ein Werkzeug, das bei
einer Störung beim Dienstleister stillsteht, wäre ein Rückschritt gegenüber
dem Zustand vor dieser Etappe.

**Der bekannte Einwand:** Wer täglich damit arbeitet, überliest Hinweise.
Deshalb ist der Hinweis eine Warnung (gelb) und steht bei der Fundliste, nicht
in der Fusszeile.

### 2.3 Was die KI ändern darf: **nur erweitern**

Sie darf eine Fundgrenze ausdehnen und im Fenster einen zusätzlichen Namen
melden. Sie darf **nie** einen Fund entfernen oder verkleinern.

**Warum:** Die Fehler sind asymmetrisch. Ein Fehlalarm kostet ein unnötig
ersetztes Wort, das ein Mensch in der Fundliste abwählt. Ein zu Unrecht
verworfener Fund kostet einen Klarnamen in einem Dokument, das aussieht wie
pseudonymisiert — und er ist unsichtbar, weil in der Liste nichts mehr steht,
das man abwählen könnte.

Dieselbe Regel gilt seit PR #8 für `propagate_findings`: *erweitern und
ergänzen, nie umbenennen* (CLAUDE.md).

**Folge:** Die drei bekannten Fehlalarme (`Kontrollschild`, `Fallnummer`,
`Intern`) bleiben. Sie sind Sachbegriffe, die ein Mensch in einer Sekunde
abwählt.

### 2.4 Auslösung: **Ankreuzfeld**, mit unterschiedlichen Vorgaben

| Weg | Vorgabe | Warum |
|---|---|---|
| Oberfläche | **an** | Bessere Erkennung soll der Normalfall sein |
| `/api/v1` und `/api/ext/v1` | **aus** | Siehe unten |

Der Maschinenpfad hängt dieselben Router ein wie die Oberfläche. Wäre die
Vorgabe „an", begännen Translator, PPM und HIKA **ab dem Deploy stillschweigend**
Dokumentinhalte hinauszuschicken, ohne dass in diesen Projekten jemand davon
wüsste. Ein Maschinen-Aufrufer muss es ausdrücklich anfordern.

## 3. Architektur

```
detect_in_segments()
  ├─ engine.analyze() je Segment          unverändert, lokal
  ├─ ki.pruefe_namen()          NEU       nur wenn angefordert
  └─ propagate_findings()                 unverändert
```

Ein einziger Einhängepunkt: `backend/pipeline.detect_in_segments`. Text- und
Dokumentpfad laufen beide dort durch.

### 3.1 Zwei Bausteine, bewusst getrennt

**`backend/ki/infomaniak.py`** — redet HTTP. Namen und Aufbau wie in
`pti-rag/backend/ki/infomaniak.py`, damit die beiden Projekte vergleichbar
bleiben und Erkenntnisse übertragbar sind.

**`backend/ki/namen.py`** — redet Text: baut Fenster, bildet die Antwort auf
`Finding`-Objekte ab, setzt die Erweiterungsregel durch.

Die Trennung ist kein Selbstzweck. Sie ist der Grund, warum ein späterer
Wechsel auf Ausbaustufe B oder C (Abschnitt 8) **nur `namen.py`** betrifft:
Client, Schema und Fehlerbehandlung bleiben unberührt.

**Der HTTP-Client wird hereingereicht, nicht im Baustein erzeugt.** Nur so sind
Zeitüberschreitung, Fehlerantwort, leere Antwort und ungültiges JSON ohne
Netzwerk prüfbar.

## 4. Der Aufruf

### 4.1 Fenster

Je Fundkandidat vom Typ `PERSON` aus dem spaCy-Erkenner: **±10 Wörter**, an
Wortgrenzen geschnitten, **nie über die Segmentgrenze hinaus**. Der Fund selbst
wird im Fenstertext markiert.

Alle Fenster eines Dokuments gehen nummeriert in **eine** Anfrage. Ein Aufruf
je Dokument statt einer je Fund — bei vierzig Namen ist das der Unterschied
zwischen einer und vierzig Wartezeiten.

Nebeneffekt, der Entscheidung 2.1 nicht verletzt: Das Modell sieht alle Fenster
nebeneinander und kann erkennen, dass Fenster 3 und Fenster 12 dieselbe Person
betreffen.

Bei sehr vielen Funden wird gestückelt. **Ab welcher Fensterzahl** ist offen
und wird als erster Schritt der Umsetzung am echten Dienst gemessen — geraten
wird sie nicht. Bis dahin gilt keine Zahl als vereinbart.

### 4.2 Strukturierte Antwort

**`response_format: {"type": "json_object"}` ergibt bei Infomaniak HTTP 400** —
gemessen am 2026-08-24. Erlaubt ist ausschliesslich
`{"type": "json_schema", "json_schema": {...}}`.

Schema je Fenster: die Nummer, der korrigierte Name, und ob es überhaupt ein
Personenname ist.

Ein `false` wird **ignoriert** (Entscheidung 2.3), aber **protokolliert**.
Daraus entsteht die Datengrundlage für die Frage, ob sich Ausbaustufe B lohnt.

### 4.3 Modell

`mistralai/Ministral-3-14B-Instruct-2512` statt der bisherigen Vorgabe
`swiss-ai/Apertus-v1.5-70B`.

Apertus ist ein Chat-Modell und steht als Vorgabe nur da, weil `pti-rag` es
benutzt. Unsere Aufgabe ist Massenklassifikation: viele kleine Anfragen, kurze
Antworten. Ministral-3 ist dafür das empfohlene Modell, eines von nur drei mit
echtem `ready`-Status, und gemessen deutlich sparsamer in der Ausgabe.

`temperature: 0` — Klassifikation braucht Wiederholbarkeit, keine Vielfalt.

Das **angefragte** Modell wird je Lauf protokolliert, nicht das aus der
Antwort: `Qwen3.5-397B` nennt sich dort `large-endpoint`.

## 5. Fehlerbehandlung

| Fall | Verhalten |
|---|---|
| Kein Schlüssel / keine Produkt-Id | KI-Prüfung meldet sich **sichtbar ab**, Rest arbeitet weiter |
| Zeitüberschreitung | Lokales Ergebnis, `ki_geprueft: false`, Warnhinweis |
| HTTP-Fehler | dito |
| **HTTP 200 mit leerem `content`** | dito — gilt als Fehler, nicht als „keine Korrektur" |
| Ungültiges JSON | dito |
| Antwort nennt eine unbekannte Fensternummer | Eintrag verworfen, protokolliert |

**Die Zeitüberschreitung wird hart gesetzt**, nicht der Vorgabewert von httpx
übernommen. Sonst wird aus „weiterlaufen bei Ausfall" ein „hängen bei Ausfall"
— derselbe Stolperstein, der beim SSO-Gate in CLAUDE.md steht.

**Niemals ein stiller Rückfall auf einen anderen Anbieter.** Die Schweizer
Verarbeitung ist der Zweck der Übung; ein Ausweichen auf OpenAI wäre genau der
Datenabfluss, der vermieden werden soll.

## 6. Vertrag nach aussen

Neues optionales Feld an `documents:detect`, `:anonymize` und
`:anonymize-file`:

- JSON: `"ki_pruefung": true`
- Multipart: Formularfeld `ki_pruefung=true`

Vorgabe **`false`** (Entscheidung 2.4). Die Antwort trägt zusätzlich
`ki_geprueft: true|false`, damit ein Aufrufer weiss, was er in der Hand hat.

In der Oberfläche ein vorangehaktes Ankreuzfeld neben der Kategorienwahl.

## 7. Messung

Der Golden-Set-Lauf misst **standardmässig ohne KI**. Die Zahl bleibt damit
vergleichbar mit allem, was seit dem 2026-08-18 gemessen wurde.

Ein zweiter, **ausdrücklich angeforderter** Lauf misst mit KI gegen den echten
Dienst. Beide Zahlen kommen ins `golden-set/README.md`.

In den Tests werden die KI-Antworten **aufgezeichnet und wiederabgespielt**.
Ohne das würde jeder Testlauf Geld kosten und die Messung von der
Verfügbarkeit eines Dienstes abhängig machen.

**Abnahmekriterium:** Das Golden-Set-Dokument `tabelle_kopf_fusszeile` findet
`Sandra Zbinden` vollständig, und der Fehlalarm auf `Sandra` verschwindet. Ohne
diesen Nachweis hat die Etappe ihr Ziel verfehlt, unabhängig davon, was die
Gesamtzahl sagt.

## 8. Ausbaustufen — was hinausgeht, und was das brächte

Auf ausdrückliche Auflage des Auftraggebers festgehalten: Reicht die
Trefferquote nicht, soll der nächste Schritt eine Entscheidung mit bekannten
Kosten sein und keine neue Grundsatzdebatte.

| Stufe | Was hinausgeht | Was das bringt | Was es kostet |
|---|---|---|---|
| **A** (dieses Design) | Fenster von ±10 Wörtern um Fundkandidaten | Korrekte Namensgrenzen; über `propagate_findings` faktisch dokumentweite Namen | Vollständig übersehene Namen bleiben übersehen |
| **B** | ganze Segmente (Absatz, Zelle, Folie) | Echte Koreferenz; auch Namen, die spaCy gar nicht meldet | Praktisch das ganze Dokument geht durch die Leitung, in Stücken |
| **C** | ganzes Dokument in einem Zug | Maximaler Kontext | Grösster Schritt beim Datenschutz, teuer bei langen Akten |
| **D** (umgesetzt 2026-08-25) | ganzes Dokument, aber **maskiert** — jeder erkannte Fund durch `[Person]`, `[Ort]`, … ersetzt | Schliesst A's blinden Fleck vollständig; **kein erkannter Name verlässt das Haus** | Der Sachverhalt geht vollständig hinaus; die Abschneide-Fehlerquelle kehrt zurück (deshalb gestückelt) |

**Nachgemessen am 2026-08-24, Anteil des Golden-Set-Textes, der hinausgeht:**

    A   66.5 %   (in namensdichten Passagen bis 371 %, weil Fenster überlappen)
    B   85.5 %
    C  100 %
    D  100 %  — davon kein einziger erkannter Name

Die Zahl für A ist die Überraschung: Fenster sind nicht automatisch sparsamer.
Stehen drei Namen in einem Satz, geht dieser Satz dreimal hinaus, einmal je
Fund.

**Warum D und nicht B oder C.** D verlangt dieselbe Textmenge wie C, gibt aber
die eine Grösse nicht preis, um die es geht. Der Rückweg ist dabei einfach: Ein
übersehener Name wurde gerade *nicht* ersetzt und steht im Original wortgleich
da — eine Textsuche genügt. Der ehrliche Haken bleibt, dass Pseudonymisieren
Namen entfernt und nicht den Sachverhalt; bei einem Polizeidokument kann der so
heikel sein wie die Namen. Der Auftraggeber hat D in Kenntnis dieses Punktes
entschieden.

**Rechtlich geklärt am 2026-08-25:** Der jCloud-Auftragsverarbeitungsvertrag
umfasst die AI Tools mit. Der Betriebsschalter `KI_ZWEITMEINUNG` ist damit kein
Notaus mehr, sondern eine reine Betriebsentscheidung.

**Nachtrag 2026-08-25 — D ist gebaut und ab Werk ausgeschaltet.** Nicht wegen
des Datenschutzes, sondern wegen einer gemessenen Nebenwirkung: D verdreifacht
die Anfragen je Dokument, der Dienst antwortet unter dieser Last mit
unbrauchbarem JSON statt mit einem sauberen HTTP 429, und dadurch fällt die
*Namensprüfung* aus — 26-mal in einem Lauf über 14 Dokumente. Am Golden-Set:

    ohne Zweitmeinung   93.2 % Precision, 100 % Recall, PERSON 100/100
    mit  Zweitmeinung   87.2 % Precision,  98.6 % Recall, PERSON 84/96

D findet also nicht das Falsche, sondern verbraucht das Kontingent, das die
funktionierende Schicht braucht. Der nächste sinnvolle Schritt ist deshalb
nicht ein anderes Modell, sondern **weniger Anfragen**: überlappende Fenster
der Namensprüfung zusammenfassen (gemessen bis 371 % Textvolumen durch
Dopplung) und die reine Ort/Organisation-Messung wieder ausbauen, sobald sie
genug Daten geliefert hat.

**Was ein Wechsel technisch kostet:** Nur der Fensterbau in `ki/namen.py`.
Client, Schema, Fehlerbehandlung, Vertrag und Oberfläche bleiben. Das ist der
Grund für die Trennung in Abschnitt 3.1.

**Woran man erkennt, dass A nicht reicht:** Am Protokoll der ignorierten
`false`-Antworten (Abschnitt 4.2) und daran, ob im Golden-Set Namen auftauchen,
die spaCy gar nicht erst meldet. Beides ist messbar, bevor jemand entscheidet.

## 9. Was in der Öffentlichkeit angepasst werden muss

Zwei Stellen behaupten heute etwas, das mit gesetztem Häkchen nicht mehr
stimmt:

1. Die Fusszeile der Anwendung (`frontend/src/App.tsx`): „Die Verarbeitung
   läuft lokal auf dem PTI-Server; es werden keine Inhalte an Dritte
   übermittelt."
2. Das Datenschutzkapitel des Dossiers
   (`landing/frontend/public/ideen/pseudonymizer.html`, §9).

Dazu die Roadmap: Punkt 3 verspricht „Zusammenfassen gleicher Personen über
das ganze Dokument" — Variante A liefert das nur für Namen, die wenigstens
einmal teilweise erkannt wurden.

**Seit Stufe D (2026-08-25) kommt ein dritter Punkt dazu, und er wiegt
schwerer als die beiden anderen:** Es geht nicht mehr nur um Ausschnitte. Der
vollständige Text jedes Dokuments wird an die AI Tools übertragen — ohne die
erkannten Namen, aber mit dem gesamten Sachverhalt. Auch ein Dokument, in dem
die lokale Erkennung **nichts** findet, geht damit hinaus; das ist gewollt,
denn genau dort ist eine Zweitmeinung etwas wert.

„Keine Inhalte an Dritte" bleibt zutreffend — die AI Tools sind ein anderes
Produkt desselben Anbieters, im selben Rechenzentrum. Ein Wortlaut, der
nahelegt, es gingen nur Ausschnitte hinaus, stimmt dagegen nicht mehr.

**Der Wortlaut wird vorgeschlagen, nicht gesetzt.** Er ist eine Aussage des
Auftraggebers gegenüber seinen Kunden, keine technische Angabe.

## 10. Ausdrücklich nicht in dieser Etappe

- **Verwerfen von Funden** durch die KI (Entscheidung 2.3)
- **Zweistufiges Vorgehen** mit Eskalation an ein stärkeres Modell — YAGNI,
  solange nicht gemessen ist, dass Ministral-3 zu schwach ist
- **Asynchrone Aufträge** — Phase 2b, unabhängig davon
- **ORGANIZATION und LOCATION** — der gemessene Fehler betrifft `PERSON`. Die
  Wortart-Regel hat gezeigt, wie teuer eine unbesehene Ausweitung auf diese
  Typen wird (sieben verlorene Funde, siehe `engine/wortarten.py`)
- **Embeddings, Vektorsuche, Reranking** — für diese Aufgabe ohne Nutzen
