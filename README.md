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
export OPENAI_MODEL='gpt-5.6-terra'
```

Important optional settings include:

- `FANTASY_DRAFT_CONFIG`, `FANTASY_DRAFT_DATABASE_URL`, `FANTASY_DRAFT_PLAYERS`
- `FANTASYPROS_DATA_MODE=auto|sample|production`
- `FANTASYPROS_TIMEOUT_SECONDS=10`
- `OPENAI_MODEL=gpt-5.6-terra`, `OPENAI_LIVE_TIMEOUT_SECONDS=25`
- `OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS=30`
- `OPENAI_REASONING_EFFORT=low`
- `OPENAI_LIVE_MODELS=gpt-5.6-terra,gpt-5.6-luna,gpt-5.6-sol`
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

The available-player table shows provider-backed age and NFL experience. `Yr` is the number of completed league years relative to the draft session's season; `R` means the player's draft year matches that season. Existing installations add these fields in place and backfill them from the latest local FantasyPros player cache during startup, without making a network request.

The DynastyProcess `db_playerids.csv` crosswalk is downloaded during refresh and cached rather than committed as a static snapshot. Source: <https://github.com/dynastyprocess/data>, GPL-3.0. Exact external IDs are matched before normalized name/position/team fallback; ambiguous records remain visible in **Data health** for review.

NFL team is mutable player metadata, not identity. A unique normalized name/position fallback may follow a player across a team change; multiple matches remain unresolved. On startup and after player import, a sample-only duplicate is retired when exactly one provider-ID-backed canonical player exists, and any non-conflicting saved picks are repointed to that canonical identity. No player record or draft history is deleted. Remaining duplicate active identities are shown in **Data health**, fail the identity readiness check, and are excluded from both deterministic and OpenAI recommendations until resolved.

After a successful pre-draft refresh, the current empty draft pins those import-run IDs. Data cannot be repinned after its first pick; create a new draft to use a newer snapshot. This preserves why a player was valued on draft night.

## Evaluation and AI

Raw projection fields flow through `scoring.py` and the draft's configuration snapshot. Provider fantasy-point totals are never authoritative. The evaluator derives replacement demand from team count, starters, configurable FLEX/SUPERFLEX eligibility, part of configured bench demand, and players already drafted; then computes VOR, tier cliffs, scarcity, and a seeded Monte Carlo next-pick survival probability. Roster utility gives diminishing weight to surplus-position depth and increasing urgency to required starters as open picks disappear.

Advisory preferences are explicit, snapshotted configuration—not hidden prompt text:

```yaml
strategy:
  rookie_late_round_bonus: 4.0
  preferred_nfl_team_bonuses: {CHI: 1.0}
  required_starter_bonus: 24.0
  surplus_position_penalty: 18.0
  replacement_bench_fraction: 0.5
  max_roster_counts: {K: 1, DEF: 1}
  position_target_rounds: {K: 17, DEF: 16}
  early_position_round_penalty: 8.0
```

Rookie preference grows quadratically through the draft and is scaled by upside/market quality. Preferred-team bonuses remain small additive tie-breakers. `max_roster_counts` is a deterministic hard guard for our team only; it does not constrain opponents. `position_target_rounds` is a soft advisory plan: before a target round the evaluator applies `early_position_round_penalty` per round and keeps one candidate visible for emergencies, while the target position becomes fully eligible in its planned round. Required roster slots still determine legality, so a configured K round 17 and DEF round 16 remain legal and cannot be crowded out by excess bench depth. New drafts snapshot the master YAML. To explicitly apply only the master strategy section to the selected setup/active draft without changing picks or league rules:

```bash
.venv/bin/fantasy-draft-ops apply-master-strategy
```

The OpenAI advisor receives a roster-aware, position-diverse set of up to 20 valid quantitative candidates plus every league roster, remaining needs, next-turn opponents, and configured strategy preferences. Required starter positions are guaranteed representation when candidates exist; surplus positions are capped. It uses the Responses API Structured Outputs schema, constrains player IDs to that request's candidate allowlist, defaults to `gpt-5.6-terra` with `low` reasoning effort, and makes no draft mutations. The automatic request happens only once the preceding pick has been saved and our team is on the clock. Successful results and credential-safe failed-attempt markers are cached for that exact draft state, so refreshes do not call the model again. Missing key, timeout, network failure, rate limit, invalid schema, or invented/drafted ID all result in the offline quantitative recommendations with a prominent `AI FALLBACK ACTIVE` warning.

Live and diagnostic latency budgets are deliberately separate. `OPENAI_LIVE_TIMEOUT_SECONDS` defaults to 25 seconds and uses zero retries, leaving time within a 30-second decision budget to read the deterministic fallback and act. `OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS` defaults to 30 seconds, also uses zero retries, bypasses the live recommendation cache, and reports configured/returned model, reasoning effort, timeout, measured latency, response status, Structured Output validation, and a credential-safe failure category. Validation diagnostics distinguish refusals, incomplete responses, missing parsed output, schema failures, and candidate-allowlist violations. The deprecated `OPENAI_TIMEOUT_SECONDS` is accepted only as a live-timeout fallback and never controls readiness. Environment changes take effect after restarting the server.

`gpt-5.6-terra` with `low` reasoning is the recommended live-draft balance. Use `gpt-5.6-luna` if the lowest latency and cost matter more than analysis quality. The `gpt-5.6` alias selects flagship `gpt-5.6-sol`; reserve it for offline analysis or testing because it is a poorer fit for a hard live clock.

The recommendation panel always provides a server-side model selector and **Try AI again**/**Re-run AI** button during an active draft. The selected model is persisted and is authoritative for the next automatic or manual request. You may manually recalculate before our turn—for example, try Sol two picks early, then select Terra for the automatic request after the pick immediately before ours. Switching models never changes deterministic draft state. Model is part of the request-cache fingerprint, so one model's cached result or timeout cannot suppress a different selected model. The enabled choices come from `OPENAI_LIVE_MODELS`; an explicit retry bypasses both successful and failed caches for the unchanged draft state.

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
