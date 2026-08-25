# Fantasy Draft AI

Fantasy Draft AI is an offline-capable live fantasy-football draft copilot for a manually maintained physical draft board. Deterministic Python code owns picks, snake order, availability, rosters, legality, scoring, persistence, and corrections. Quantitative analysis ranks valid candidates; the LLM is a bounded advisory layer that can disappear without breaking the draft.

## Current capabilities

- Persistent setup, active, completed, and archived draft sessions
- Safe reset: archive the old session and create an empty copy
- Immutable league and data-source snapshots per draft
- Config-driven snake order, roster legality, FLEX/SUPERFLEX eligibility, and scoring
- Canonical application-owned player IDs plus provider external-ID mappings
- Explicit FantasyPros players, consensus rankings, ADP, and preseason-projection refresh
- DynastyProcess/nflverse ID crosswalk enrichment with unmatched-player review
- League scoring from raw projected stats, including configured yardage bonuses
- Explainable VOR, replacement levels, calculated tiers, scarcity, survival simulation, and cost of waiting
- Strict OpenAI Structured Output with candidate-ID validation, timeout, result cache, and deterministic fallback
- Immediate transactional pick persistence, correction, and undo
- JSON session export, picks CSV, consistent SQLite backup, and readiness checks
- Practice/rehearsal sessions using the same production draft flow

The bundled `data/players.csv` is an illustrative offline fallback, not a current ranking source.

## Install and start

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/fantasy-draft --reload
```

Open <http://127.0.0.1:8000>. If the browser reports that `127.0.0.1` refused the connection, the server is not running in that shell; start the command above and leave it running. For the real draft, omit `--reload`:

```bash
.venv/bin/fantasy-draft
```

The ignored SQLite database is created at `instance/fantasy_draft.db`. Startup applies forward-only schema upgrades; do not delete the database when the schema or league configuration changes.

## League configuration and draft sessions

`config/league.yaml` is the editable master configuration derived from `league-settings.txt`. It is sample data, never an application rule. It controls team count, snake rounds, teams and slots, roster positions, flex eligibility, and all scoring categories.

The app reads the master YAML whenever it creates a new draft. Every draft receives its own validated YAML snapshot. Therefore:

- after editing YAML, open **Draft sessions → New Draft** to use the new settings;
- an existing or archived draft intentionally keeps its original settings;
- **Reset safely** archives the session and creates an empty copy with the same snapshot;
- use **New Draft**, not reset, when the new master YAML should apply;
- no application restart is required merely to edit YAML.

Use `practice` sessions for rehearsal and `live` for draft night. Pick entry, recommendations, undo, correction, export, and reset behave identically.

```bash
.venv/bin/fantasy-draft-ops new-draft --name "Draft-night rehearsal" --kind practice
```

## Environment variables

The application reads process environment variables and never exposes provider keys to the browser. Copy the names in `.env.example` into your shell/process manager; `.env` files are ignored but are not loaded automatically.

```bash
export FANTASYPROS_API_KEY='...'
export OPENAI_API_KEY='...'
export OPENAI_MODEL='gpt-5.6'
```

Important optional settings include:

- `FANTASY_DRAFT_CONFIG`, `FANTASY_DRAFT_DATABASE_URL`, `FANTASY_DRAFT_PLAYERS`
- `FANTASYPROS_DATA_MODE=auto|sample|production`
- `FANTASYPROS_TIMEOUT_SECONDS=10`
- `OPENAI_MODEL=gpt-5.6`, `OPENAI_LIVE_TIMEOUT_SECONDS=5`
- `OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS=30`
- `OPENAI_REASONING_EFFORT=low`, `OPENAI_PREFETCH_PICKS=3`
- `SURVIVAL_SIMULATIONS=2000`

Changing from a test FantasyPros key to a production key requires no code change. When response metadata does not prove that data is production, the UI reports `UNKNOWN`; it never silently labels data as current production. Set `FANTASYPROS_DATA_MODE=sample` for the free prototype key if its payload does not self-identify.

## Player data and provider refresh

The server-side provider uses the official FantasyPros Public API v2 base URL and `x-api-key` header:

- `GET /nfl/players` with requested external IDs
- `GET /nfl/{season}/consensus-rankings` for DRAFT and ADP, including range/dispersion fields
- `GET /nfl/{season}/projections?week=0` by configured draftable position

Refresh is explicit—never part of page rendering—and writes versioned gzip source caches below `instance/cache/`. Failed refreshes create audited failed import records and retain the prior local data. Refresh from **Data health** or:

```bash
.venv/bin/fantasy-draft-ops refresh-data
```

The DynastyProcess `db_playerids.csv` crosswalk is downloaded during refresh and cached rather than committed as a static snapshot. Source: <https://github.com/dynastyprocess/data>, GPL-3.0. Exact external IDs are matched before normalized name/position/team fallback; ambiguous records remain visible in **Data health** for review.

After a successful pre-draft refresh, the current empty draft pins those import-run IDs. Data cannot be repinned after its first pick; create a new draft to use a newer snapshot. This preserves why a player was valued on draft night.

## Evaluation and AI

Raw projection fields flow through `scoring.py` and the draft's configuration snapshot. Provider fantasy-point totals are never authoritative. The evaluator derives replacement demand from team count, starters, configurable FLEX/SUPERFLEX eligibility, and part of configured bench demand; then computes VOR, tier cliffs, scarcity, and a seeded Monte Carlo next-pick survival probability.

The OpenAI advisor receives only the top 20 valid quantitative candidates plus concise league/draft/roster/opponent context. It uses the Responses API Structured Outputs schema, defaults to `gpt-5.6` with `low` reasoning effort, makes no draft mutations, caches identical live-draft state, and starts prefetching as our pick approaches. Missing key, timeout, network failure, rate limit, invalid schema, or invented/drafted ID all result in the offline quantitative recommendations.

Live and diagnostic latency budgets are deliberately separate. `OPENAI_LIVE_TIMEOUT_SECONDS` defaults to five seconds and uses zero retries so draft decisions fall back immediately. `OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS` defaults to 30 seconds, also uses zero retries, bypasses the live recommendation cache, and reports configured/returned model, reasoning effort, timeout, measured latency, Structured Output validation, and a credential-safe failure category. The deprecated `OPENAI_TIMEOUT_SECONDS` is accepted only as a live-timeout fallback and never controls readiness.

## Readiness, export, and backup

Run the prominent **Readiness** workflow in the UI or:

```bash
.venv/bin/fantasy-draft-ops readiness
.venv/bin/fantasy-draft-ops readiness --offline
```

The diagnostic check validates configuration, database writes, data freshness/mode, identity coverage, scoring/VOR/tiers/survival, provider connectivity, AI Structured Output when configured, deterministic fallback, exports, backup location, snake order, and an isolated pick/undo/correction rehearsal. OpenAI timeouts are categorized as connect, read, write, pool, or unknown timeouts. Warnings such as missing/slow AI or sample data do not disable manual drafting; failures are visibly separate.

Download JSON or CSV from the draft board/session list, or run:

```bash
.venv/bin/fantasy-draft-ops export --format json --output instance/exports/draft.json
.venv/bin/fantasy-draft-ops export --format csv --output instance/exports/picks.csv
.venv/bin/fantasy-draft-ops backup
```

JSON contains metadata, configuration and provider-run snapshots, draft order, every selection, reconstructed rosters, canonical/external IDs, and persisted recommendation history. CSV is a human-friendly pick list. Neither contains API credentials.

SQLite backups use SQLite's online backup API and are stored under `instance/backups/`. To restore: stop the app, retain the current database as a safety copy, copy the chosen backup over the configured SQLite database path, then restart and confirm `/health` and **Readiness**. Never replace a running database file.

## API

- `GET /api/state` — deterministic state, analytics, and advisory recommendations
- `POST /api/picks` with `{"player_id":"..."}` — durably save the current pick
- `PUT /api/picks/{overall}` — correct a saved pick
- `DELETE /api/picks/last` — undo the last pick
- `GET /drafts/{id}/export.json` and `.csv` — session exports
- `GET /health` — database reachability

Interactive API documentation is at <http://127.0.0.1:8000/docs>.

## Tests

Routine tests are deterministic and never require internet access:

```bash
.venv/bin/pytest
```

The explicit integration diagnostics are `fantasy-draft-ops refresh-data` and `fantasy-draft-ops readiness`; they make live calls only when the corresponding environment keys are configured.

## Architecture

- `engine.py`: pure draft/roster rules
- `services.py`: transactional session lifecycle and state reconstruction
- `models.py` / `migrations.py`: persistent canonical data and forward schema upgrades
- `providers.py`, `identity.py`, `data_import.py`: external data boundaries and audited matching
- `scoring.py`, `evaluation.py`: league scoring and deterministic analytics
- `llm.py`: strict strategic advisor above the candidate set
- `operations.py`, `readiness.py`: exports, backup, and operational validation
- `web.py`: thin FastAPI/server-rendered UI adapter

The intended dependency direction is external data → canonical players → league scoring → deterministic analytics → valid candidates → optional LLM → human decision.
