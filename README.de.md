# agy-dap — Hypothesengesteuertes Debug Adapter Protocol (DAP) Plugin für Antigravity CLI

[![Tests](https://img.shields.io/badge/tests-34%20passed-brightgreen)](#12-getestete-versionen-plattformen--ci)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](#12-getestete-versionen-plattformen--ci)
[![MCP](https://img.shields.io/badge/MCP-compatible-orange)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*[English](README.md) | [Deutsch](README.de.md)*

**agy-dap** ist ein lokales, agentenfreundliches Debugging-Plugin für die **Google Antigravity CLI (`agy`)**. Es verbindet KI-Agenten über das **Debug Adapter Protocol (DAP)** mit Pythons **`debugpy`**-Debugger und stellt kompakte, deterministische Werkzeuge zur Laufzeitinspektion über das **Model Context Protocol (MCP)** via `stdio` bereit.

Anstatt allein aus statischem Quellcode und Test-Logs auf das Laufzeitverhalten zu schließen, kann der Agent zu einem fehlschlagenden `pytest`-Test eine gezielte Hypothese formulieren, einen bestätigten Haltepunkt setzen und Stack-Frames sowie lokale Variablen in einem einzigen, zeitlich begrenzten Aufruf inspizieren.

---

## Inhaltsverzeichnis

1. [Zielsetzung & Nicht-Ziele](#1-zielsetzung--nicht-ziele)
2. [Architektur & Session-Lifecycle](#2-architektur--session-lifecycle)
3. [Sicherheits- & Vertrauensmodell](#3-sicherheits---vertrauensmodell)
4. [Installation & Setup](#4-installation--setup)
   - [Windows (PowerShell)](#windows-powershell)
   - [Linux / macOS (Bash)](#linux--macos-bash)
5. [Antigravity CLI & MCP-Integration](#5-antigravity-cli--mcp-integration)
6. [Konfiguration (`agy-dap.json`)](#6-konfiguration-agy-dapjson)
7. [MCP-Werkzeuge (Referenz)](#7-mcp-werkzeuge-referenz)
8. [Reproduzierbarer Durchlauf am Fixture-Projekt](#8-reproduzierbarer-durchlauf-am-fixture-projekt)
9. [Zusammenspiel mit `agy-lsp`](#9-zusammenspiel-mit-agy-lsp)
10. [Fehlervertrag & Fehlerbehebung](#10-fehlervertrag--fehlerbehebung)
11. [Messwerte & Evaluationsmethodik](#11-messwerte--evaluationsmethodik)
12. [Getestete Versionen, Plattformen & CI](#12-getestete-versionen-plattformen--ci)
13. [Bekannte Einschränkungen & Ausblick](#13-bekannte-einschränkungen--ausblick)
14. [Offizielle Referenzen](#14-offizielle-referenzen)

---

## 1. Zielsetzung & Nicht-Ziele

### Hypothesengesteuerter Ablauf
1. Der Agent benennt einen reproduzierbar fehlschlagenden Test und formuliert eine zu prüfende Laufzeithypothese.
2. Er wählt eine relevante Quelldatei/Zeile als Haltepunkt sowie die exakt benötigten lokalen Variablennamen.
3. `agy-dap` startet den Test über `debugpy.adapter` anhand eines expliziten Projektprofils.
4. Die Ausführung hält am bestätigten Breakpoint oder bei einer konfigurierten Exception.
5. `agy-dap` liefert einen kompakten, strukturierten Snapshot (`stack`, gefilterte `locals`, gefalteten `output_tail` und `breakpoints`).
6. Der Agent entscheidet anhand der tatsächlichen Beobachtungen über den nächsten Schritt.
7. Nach einer Codekorrektur wird der Test außerhalb des Debuggers regulär mit `pytest` erneut geprüft.

`agy-dap` liefert **Laufzeitbeobachtungen, keine erfundenen Ursachenanalysen** und nimmt keine automatischen Codeänderungen vor.

### Nicht Teil des MVPs
- Kein Remote-Debugging oder Attach an fremde Prozesse (PIDs).
- Keine Produktivsysteme, Container-Orchestrierung oder Browser-Debugging.
- Python (`debugpy` + `pytest`) als einzige Zielsprache im MVP.
- Keine freien Shell-Kommandos oder beliebigen Executables über Tool-Parameter.
- Keine schreibenden Debuggeroperationen (`setVariable`, `setExpression`).
- Kein Reverse Debugging / Time-Travel und keine unbegrenzten Objektgraphen.

---

## 2. Architektur & Session-Lifecycle

```text
┌───────────────────────────┐
│   Antigravity Agent / CLI │
└─────────────┬─────────────┘
              │ MCP (stdio)
┌─────────────▼─────────────┐
│    agy-dap MCP Server     │
│  ┌─────────────────────┐  │
│  │   SessionManager    │  │
│  │ (Lifecycle, Locks,  │  │
│  │  Idle Watchdog)     │  │
│  └──────────┬──────────┘  │
│  ┌──────────▼──────────┐  │
│  │     DapClient       │  │
│  │ (Framing, Handshake,│  │
│  │  Snapshot Builder)  │  │
│  └──────────┬──────────┘  │
└─────────────┼─────────────┘
              │ DAP (Content-Length stdio)
┌─────────────▼─────────────┐
│      debugpy.adapter      │
│             │             │
│             ▼             │
│   pytest Debuggee Process │
└───────────────────────────┘
```

### Paketstruktur

```text
src/agy_dap/
  server.py          # MCP stdio-Server & Tool-Registrierung
  config.py          # Versioniertes Schema (1.0), Profile, Limits, Timeouts
  models.py          # Breakpoint-, StackFrame-, Variable- und StopEvent-Modelle
  errors.py          # Maschinenlesbarer Fehlervertrag (17 Fehlercodes)
  symbols.py         # Optionales SymbolResolver-Protokoll für künftige LSP-Anbindung
  dap/
    transport.py     # Bytekorrektes Content-Length-Framing & Single-Reader-Transport
    client.py        # Request-Korrelation, asynchrone Event-Queues, Launch-Handshake
    capabilities.py  # Auswertung und Durchsetzung der Adapter-Capabilities
  adapters/
    base.py          # Minimale Adapter-Abstraktion
    debugpy.py       # debugpy.adapter Subprozess-Starter & pytest-Launch-Konfiguration
  sessions/
    lifecycle.py     # Zustandsautomat & Stop-Generation-Prüfung gegen veraltete Referenzen
    manager.py       # Session-Verwaltung, Serialisierung, Idle-Timeout, Prozess-Cleanup
  inspection/
    snapshots.py     # Kompakter Snapshot-Builder mit Byte-Budget & optionalen Code-Snippets
    variables.py     # Scopes/Variables-Inspektion, Falsy-Erhalt, Redaction
    output.py        # Begrenzter Output-Ringpuffer mit Faltung wiederholter Zeilen
  tools/
    doctor.py        # debug_doctor Vorabprüfung
    inspect.py       # debug_inspect gebündelter Einmal-Aufruf
    sessions.py      # Zustandsbehaftete Tools (debug_start, debug_snapshot, debug_continue, debug_evaluate, debug_stop)
  utils/
    paths.py         # Workspace-Pfadprüfung & pytest-Selektor-Validierung
    processes.py     # Bereinigte Env-Allowlist & plattformgerechtes Prozessbaum-Cleanup
    redaction.py     # Best-Effort-Maskierung vertraulicher Schlüssel und Token
    logging.py       # Stderr-Logger mit automatischer Redaction
```

### Session-Zustandsautomat
```text
CREATED → STARTING → CONFIGURING → RUNNING ⇄ STOPPED
                                      ↓
                         TERMINATING → TERMINATED
```
- **Stop-Generation / Snapshot-ID**: Jeder Übergang nach `STOPPED` erhöht die Stop-Generation (`snapshot_id = snap_<id>_<gen>`). Veraltete `frame_id`- oder `snapshot_id`-Referenzen nach einem `continue` / Step werden deterministisch mit `STALE_SNAPSHOT` abgewiesen.
- **DAP-Handshake ohne Race Conditions**: Ausstehende Requests werden vor dem Schreiben auf `stdin` registriert; asynchrone Events (`initialized`, `stopped`, `terminated`) werden über Event-Objekte und Queues ohne feste Sleeps korreliert, unabhängig davon, ob die `launch`-Response vor oder nach `configurationDone` eintrifft.

---

## 3. Sicherheits- & Vertrauensmodell

### Lokale Codeausführung
Debugging führt lokalen Projekt- und Testcode aus. **Debugge ausschließlich vertrauenswürdige Projekte.** Pfadprüfungen und deaktiviertes `evaluate` machen den Python-Interpreter nicht zu einer Sandbox.

- **Benannte Launch-Profile**: Starts erfolgen ausschließlich über definierte Profile (`"pytest"`). Freie Shell-Kommandos oder beliebige Binärpfade über Tool-Parameter sind ausgeschlossen.
- **Explizite Argumentlisten**: Subprozesse werden über `asyncio.create_subprocess_exec` (`shell=False`) gestartet. Reverse Requests wie `runInTerminal` werden strukturiert abgelehnt.
- **Validierung von Test-Selektoren**: Selektoren (`tests/test_file.py::test_name`) werden strukturell geprüft. Führende Bindestriche (`-o`, `--rootdir`), Steuerzeichen, Nicht-`.py`-Dateien und Pfade außerhalb des Workspaces (auch über `..` oder Symlinks) werden mit `INVALID_TEST_SELECTOR` bzw. `PATH_OUTSIDE_WORKSPACE` abgewiesen.

### Variableninspektion & Ausdrucksauswertung
- **Sichere Standard-Inspektion**: Snapshots nutzen ausschließlich DAP `scopes`- und `variables`-Anfragen. Der Parameter `locals` ist ein **Namensfilter**, keine Ausdrucksliste.
- **`debug_evaluate` standardmäßig deaktiviert**: Freie Ausdrücke in `debug_evaluate` können in Python beliebigen Code ausführen (auch im DAP-Kontext `"watch"`) und sind serverseitig blockiert (`EVALUATION_DISABLED`), sofern nicht explizit `"enableEvaluation": true` in `agy-dap.json` gesetzt wurde.
- **Hinweis zu `__repr__`**: Auch ohne `evaluate` ruft `debugpy` beim Formatieren von Objekten in `variables` deren `__repr__`-Methode im Debuggee auf. `agy-dap` setzt standardmäßig `justMyCode: true` und `showReturnValue: false`; projektspezifische `__repr__`-Methoden mit Seiteneffekten können beim Inspizieren des jeweiligen Frames dennoch ausgeführt werden.

### Vertrauliche Daten & Umgebungshygiene
- **Umgebungs-Allowlist**: `agy-dap` kopiert nicht ungefiltert `os.environ` in den Debuggee-Prozess. Nur notwendige Laufzeitvariablen (`PATH`, `SYSTEMROOT`, `HOME`, `VIRTUAL_ENV` etc.) sowie explizit in `envAllowlist` freigegebene Schlüssel werden übergeben; Variablen mit Secret-Namensmustern werden ausgefiltert.
- **Best-Effort-Redaction**: Variablennamen mit Mustern wie `password`, `secret`, `token`, `api_key`, `authorization`, `client_secret` sowie Strings mit Inline-Tokens (`sk-...`, `ghp_...`, `Bearer ...`) werden durch `"[REDACTED]"` (`status: "redacted"`) ersetzt. Dies ist eine Schutzmaßnahme nach bestem Bemühen, keine formale Garantie.
- **Keine persistenten Dumps**: Snapshots und Variablenwerte werden standardmäßig nicht auf die Festplatte geschrieben.

---

## 4. Installation & Setup

### Windows (PowerShell)

```powershell
cd D:\Projekte\Plugins\agy-dap

# 1. Virtuelle Umgebung erstellen
python -m venv .venv

# 2. agy-dap inklusive Entwicklungs- und Testabhängigkeiten installieren
.\.venv\Scripts\pip.exe install -e ".[dev]"

# 3. Tests ausführen
.\.venv\Scripts\pytest.exe -v
```

### Linux / macOS (Bash)

```bash
cd /pfad/zu/agy-dap

# 1. Virtuelle Umgebung erstellen
python3 -m venv .venv
source .venv/bin/activate

# 2. agy-dap inklusive Entwicklungs- und Testabhängigkeiten installieren
pip install -e ".[dev]"

# 3. Tests ausführen
pytest -v
```

---

## 5. Antigravity CLI & MCP-Integration

### Option A: Als Antigravity-Plugin
Kopiere oder verlinke `agy-dap` in eines der Plugin-Verzeichnisse der Antigravity CLI:
- **Projektbezogen**: `.agents/plugins/agy-dap`
- **Benutzerweit**: `~/.gemini/config/plugins/agy-dap`

Validierung des Plugin-Manifests:
```powershell
agy plugin validate .
```
Verifizierte Ausgabe:
```text
Validating agy-dap (D:\Projekte\Plugins\agy-dap\plugin.json)

  skills 1 · agents 0 · rules 1 · hooks 0 · mcpServers 1 · sidecars 0

✔ Validation passed
```

### Option B: Direkte Registrierung als MCP-Server
```powershell
agy mcp add agy-dap -- command="python" args=["/pfad/zu/agy-dap/run_server.py"]
```
`run_server.py` wechselt automatisch in das lokale `.venv` des Plugins (falls vorhanden) und schreibt interne Logs ausschließlich nach `stderr`.

---

## 6. Konfiguration (`agy-dap.json`)

Lege optional eine Datei `agy-dap.json` im Workspace-Root (oder `.gemini/agy-dap.json`) an. Eine vollständige Vorlage ohne lokale Benutzerpfade befindet sich in [`config.default.json`](config.default.json):

```json
{
  "schemaVersion": "1.0",
  "workspaceRoot": null,
  "pythonPath": null,
  "allowOutsideWorkspace": false,
  "enableEvaluation": false,
  "includeCodeSnippets": false,
  "snippetContextLines": 3,
  "maxConcurrentSessions": 1,
  "envAllowlist": [],
  "limits": {
    "maxStackFrames": 8,
    "maxVariablesPerScope": 25,
    "maxVariableDepth": 2,
    "maxContainerItems": 10,
    "maxStringLength": 240,
    "maxOutputLines": 40,
    "maxOutputLineLength": 300,
    "maxOutputBufferLines": 500,
    "maxSnapshotBytes": 16384
  },
  "timeouts": {
    "startupTimeoutMs": 15000,
    "requestTimeoutMs": 10000,
    "defaultInspectTimeoutSec": 30.0,
    "maxInspectTimeoutSec": 120.0,
    "idleSessionTimeoutSec": 120.0,
    "cleanupTimeoutSec": 5.0
  },
  "profiles": {
    "pytest": {
      "runner": "pytest",
      "pythonPath": null,
      "module": "pytest",
      "cwd": null,
      "args": ["-q", "-s", "--no-header", "--tb=short"],
      "env": {},
      "justMyCode": true,
      "stopOnUncaughtException": true,
      "stopOnRaisedException": false
    }
  }
}
```

---

## 7. MCP-Werkzeuge (Referenz)

| Werkzeug | Zweck | Wichtige Argumente | Verhalten |
| :--- | :--- | :--- | :--- |
| `debug_doctor` | Prüft Workspace, Python-Interpreter, `debugpy`, `pytest`, Profile und Konfiguration | `profile="pytest"` | Nur-lesende Prüfung; liefert `healthy`, Versionen und konkrete `remediations`. |
| `debug_inspect` | **Primärer Einmal-Aufruf** | `test_id`, `breakpoint={"file": ..., "line": ...}`, `profile="pytest"`, `locals=[...]`, `timeout_seconds=30`, `include_snippet=None` | Startet Session, wartet auf ersten Halt (Breakpoint/Exception), erstellt Snapshot (optional mit Code-Snippet) und beendet die Session zuverlässig vor der Rückgabe. |
| `debug_start` | Startet zustandsbehaftete Untersuchung | `test_id`, `breakpoint`, `profile="pytest"`, `locals=[...]`, `timeout_seconds=30`, `include_snippet=None` | Hält am ersten Breakpoint/Exception, liefert `session_id` + Snapshot und hält die Session für Folgeaufrufe offen (inkl. Idle-Timeout). |
| `debug_snapshot` | Snapshot einer angehaltenen Session | `session_id`, `thread_id=None`, `frame_id=None`, `locals=[...]`, `snapshot_id=None`, `include_snippet=None` | Liefert Stack & Variablen einer `STOPPED`-Session. Nutzt bevorzugt den Thread des Stop-Events. Weist veraltete Referenzen mit `STALE_SNAPSHOT` ab. |
| `debug_continue` | Fortsetzen oder Einzelschritt | `session_id`, `action="continue" \| "next" \| "step_in" \| "step_out"`, `locals=[...]`, `timeout_seconds=None`, `include_snippet=None` | Führt die gewünschte Aktion aus, wartet auf den nächsten Halt oder das Prozessende und liefert den neuen Snapshot. |
| `debug_evaluate` | Optionale Ausdrucksauswertung | `session_id`, `expression`, `frame_id=None`, `snapshot_id=None` | **Standardmäßig deaktiviert** (`EVALUATION_DISABLED`). Erfordert `"enableEvaluation": true` in `agy-dap.json`. |
| `debug_stop` | Beendet eine Session idempotent | `session_id` | Beendet Debuggee- und Adapter-Prozessbaum und meldet den bestätigten Cleanup-Zustand. |

---

## 8. Reproduzierbarer Durchlauf am Fixture-Projekt

Das Repository enthält unter [`tests/fixtures/sample_pytest_project`](tests/fixtures/sample_pytest_project) ein eigenständiges Beispielprojekt mit dem reproduzierbar fehlschlagenden Test `tests/test_checkout.py::test_discount`.

### Schritt 1: Aufruf von `debug_inspect`
```json
{
  "profile": "pytest",
  "test_id": "tests/test_checkout.py::test_discount",
  "breakpoint": { "file": "src/checkout.py", "line": 40 },
  "locals": ["subtotal", "discount", "running_total", "tax", "is_tax_exempt", "empty_memo", "coupon_code", "api_token"],
  "timeout_seconds": 30
}
```

### Schritt 2: Gelieferter kompakter Snapshot
```json
{
  "session_id": "sess_a1b2c3d4e5f6",
  "snapshot_id": "snap_c3d4e5f6_1",
  "state": "STOPPED",
  "stop_reason": "breakpoint",
  "outcome": "breakpoint_hit",
  "thread_id": 1,
  "frame_id": 2,
  "location": {
    "file": "src/checkout.py",
    "line": 40,
    "function": "calculate_total"
  },
  "stack": [
    { "frame_id": 2, "function": "calculate_total", "file": "src/checkout.py", "line": 40, "column": 1 },
    { "frame_id": 3, "function": "test_discount", "file": "tests/test_checkout.py", "line": 14, "column": 1 }
  ],
  "locals": {
    "subtotal": { "value": 100.0, "type": "float", "status": "ok" },
    "discount": { "value": 15.0, "type": "float", "status": "ok" },
    "running_total": { "value": 115.0, "type": "float", "status": "ok" },
    "tax": { "value": 0, "type": "int", "status": "ok" },
    "is_tax_exempt": { "value": false, "type": "bool", "status": "ok" },
    "empty_memo": { "value": "", "type": "str", "status": "ok" },
    "coupon_code": { "value": null, "type": "NoneType", "status": "null" },
    "api_token": { "value": "[REDACTED]", "type": "str", "status": "redacted" }
  },
  "breakpoints": [
    { "requested_file": "src/checkout.py", "requested_line": 40, "verified": true, "actual_line": 40, "id": 0 }
  ],
  "output_tail": [
    "checkout-audit-step: validating cart line (repeated 4x)"
  ],
  "truncated": true,
  "session_terminated": true,
  "cleanup": { "clean": true, "state": "TERMINATED" }
}
```
Anhand von `subtotal = 100.0`, `discount = 15.0` und `running_total = 115.0` in Zeile 40 ist die Hypothese sofort bestätigt: In Zeile 39 (`running_total = round(subtotal + discount, 2)`) wurde der Rabatt addiert statt subtrahiert.

---

## 9. Zusammenspiel mit `agy-lsp`

- `agy-dap` hat **keine Laufzeitabhängigkeit** von einem laufenden `agy-lsp`-Server; Datei- und Zeilen-Breakpoints funktionieren eigenständig.
- Wenn ein Agent einen Haltepunkt anhand eines Symbolnamens wählen möchte:
  1. Über `agy-lsp` (`go_to_definition` oder `document_symbols`) Datei und Deklarationszeile ermitteln.
  2. Eine ausführbare Zeile im Funktionskörper wählen und `{ "file": ..., "line": ... }` an `agy-dap` übergeben.
  3. In der Antwort `breakpoints[0].actual_line` und `verified` prüfen, da die `def`-Kopfzeile einer Funktion nicht der Punkt ist, an dem lokale Variablen bereits berechnet sind.
- Programmatische Schnittstelle: [`src/agy_dap/symbols.py`](src/agy_dap/symbols.py) definiert das `SymbolResolver`-Protokoll ohne fehleranfällige Regex-Parser.

---

## 10. Fehlervertrag & Fehlerbehebung

Alle Fehler folgen einem einheitlichen, maschinenlesbaren JSON-Schema ohne interne Stacktraces:

```json
{
  "error": {
    "code": "BREAKPOINT_UNVERIFIED",
    "message": "Breakpoint at src/checkout.py:9999 was not verified by the debug adapter.",
    "retryable": false,
    "details": {}
  }
}
```

| Fehlercode | Ursache & Abhilfe |
| :--- | :--- |
| `DEPENDENCY_MISSING` | Python-Interpreter, `debugpy` oder `pytest` fehlt. `debug_doctor` liefert den konkreten Installationsbefehl. |
| `INVALID_CONFIG` | Ungültige `agy-dap.json`, unbekanntes Profil oder ungültige Tool-Parameter. |
| `PATH_OUTSIDE_WORKSPACE` | Breakpoint-Datei, Testdatei oder `cwd` liegt außerhalb von `workspaceRoot`. |
| `INVALID_TEST_SELECTOR` | Selektor beginnt mit `-` (Flag-Injection), verweist nicht auf eine `.py`-Datei oder hat ungültige Syntax. |
| `CAPABILITY_UNSUPPORTED` | Angeforderte Funktion wird vom Debug-Adapter nicht unterstützt. |
| `BREAKPOINT_UNVERIFIED` | Breakpoint konnte vom Adapter nicht bestätigt werden (z. B. Zeile hinter Dateiende). |
| `TARGET_NOT_REACHED` | Test endete (`exit_code` in `details`), ohne den Breakpoint zu erreichen. |
| `SESSION_NOT_FOUND` | Unbekannte oder bereits beendete `session_id`. |
| `SESSION_NOT_STOPPED` | Inspektions- oder Step-Aufruf auf einer Session, die nicht im Zustand `STOPPED` ist. |
| `STALE_SNAPSHOT` | `snapshot_id` oder `frame_id` stammt aus einem früheren Halt vor `continue` / Step. |
| `EVALUATION_DISABLED` | `debug_evaluate` aufgerufen, während `enableEvaluation` deaktiviert ist (Standard). |
| `CONCURRENT_SESSION_LIMIT` | Maximale Anzahl gleichzeitiger Sessions erreicht (`maxConcurrentSessions: 1`). Zuerst `debug_stop` aufrufen. |
| `TIMEOUT` | Zeitüberschreitung bei Adapter-Anfrage oder Testlauf; Prozesse wurden bereinigt. |
| `ADAPTER_CRASHED` | Adapter-Prozess unerwartet beendet oder Transport geschlossen. |
| `PROTOCOL_ERROR` | Ungültige DAP-Nachricht oder vom Adapter abgelehnte Anfrage. |
| `CLEANUP_FAILED` | Terminierung des Prozessbaums konnte innerhalb von `cleanupTimeoutSec` nicht vollständig bestätigt werden. |

---

## 11. Messwerte & Evaluationsmethodik

Das reproduzierbare Benchmark-Skript kann lokal gegen das Fixture-Projekt ausgeführt werden:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py
```

### Gemessene lokale Ergebnisse (Windows 11, Python 3.14.8, debugpy 1.8.22, pytest 9.1.1)
- **Launch-bis-Snapshot-Latenz**: ~4.680 ms
- **MCP-Tool-Aufrufe**: 1 (`debug_inspect`)
- **Gesendete DAP-Requests**: 9 (`initialize`, `launch`, `setBreakpoints`, `setExceptionBreakpoints`, `configurationDone`, `threads`, `stackTrace`, `scopes`, `variables`)
- **Kompakter gefilterter Snapshot**: `1.013` UTF-8-Bytes (`254` geschätzte Tokens via `ceil(chars / 4)`)
- **Ungekürzter Roh-Capture am selben Haltepunkt**: `12.047` UTF-8-Bytes (`3.012` geschätzte Tokens via `ceil(chars / 4)`)
- **Größenverhältnis (Single-Snapshot)**: `0,0841` (der kompakte Snapshot beträgt ~8,4 % des ungekürzten Captures am selben Stop-Punkt)

*Methodischer Hinweis:* Die Token-Angaben sind zeichenbasierte Schätzungen (`ceil(chars / 4)`) und keine exakten Tokenizer-Messungen eines spezifischen Modells. Zudem beweist ein einzelner kompakter Snapshot noch keine Netto-Tokenersparnis über eine vollständige Agenten-Session hinweg. Für eine belastbare End-to-End-Evaluation sollten gepaarte A/B-Läufe auf einer festen Suite reproduzierbarer Fehlerszenarien (mit und ohne `agy-dap`) durchgeführt und Erfolgsquote, Zeit bis zum grünen Test, Anzahl der Agenten-Turns sowie die tatsächlichen API-Tokenzahlen verglichen werden.

---

## 12. Getestete Versionen, Plattformen & CI

### Lokal verifizierte Umgebung
- **Betriebssystem**: Windows (win32, AMD64)
- **Python**: `3.14.8`
- **debugpy**: `1.8.22`
- **pytest**: `9.1.1` (`pytest-asyncio 1.4.0`)
- **MCP Python SDK**: `2.3.0`
- **Ruff**: `0.17.1`
- **Antigravity CLI**: `agy` (`agy plugin validate` erfolgreich)

### Konfigurierte CI-Matrix
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) definiert Jobs für `ubuntu-latest` und `windows-latest` unter Python `3.11` und `3.12` (Ruff, Unit-Tests, echte `debugpy`-Integrations-Tests und `scripts/benchmark.py`). Hinweis: Die CI-Workflow-Datei ist vorbereitet; direkt ausgeführt wurde in dieser Umgebung die lokale Windows-Testsuite.

---

## 13. Bekannte Einschränkungen & Ausblick

- **Nur Python / pytest im MVP**: Im MVP sind ausschließlich `debugpy` und der `pytest`-Runner freigeschaltet.
- **Standardmäßig eine aktive Session**: `maxConcurrentSessions` ist im Standard auf `1` gesetzt, um verwaiste Debugger-Prozesse zu verhindern.
- **Kein Subprozess-Debugging (`subProcess: false`)**: Für deterministisches Cleanup läuft `pytest` im MVP in einem einzelnen Debuggee-Prozess.
- **Ausblick**: Weitere Runner-Profile (z. B. `unittest` oder Modul-Profile), optionales Multi-Process-Debugging sowie eine optionale Anbindung des `SymbolResolver`-Protokolls an `agy-lsp`.

---

## 14. Offizielle Referenzen

- [Debug Adapter Protocol (DAP) Overview](https://microsoft.github.io/debug-adapter-protocol/overview.html)
- [Debug Adapter Protocol (DAP) Specification](https://microsoft.github.io/debug-adapter-protocol/specification)
- [microsoft/debugpy](https://github.com/microsoft/debugpy)
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- [k0pfnicker/agy-lsp (Integrations-Referenz)](https://github.com/k0pfnicker/agy-lsp)
- [microsoft/DebugMCP (Referenz)](https://github.com/microsoft/DebugMCP)
