# Fantasy Draft AI Engineering Guide

## Product focus

The current product is a fast, dependable copilot for a manually entered live fantasy-football draft. Build season-management features only when they are explicitly in scope. Preserve extension points for later waivers, lineups, injuries, trades, and scheduled analysis without prematurely implementing them.

## Permanent architectural rules

- Never hard-code league-specific rules in Python, templates, or JavaScript. Read validated structured configuration. `config/league.yaml` is sample configuration, not universal truth.
- Deterministic application code owns draft order, snake logic, pick numbers, ownership, player availability, roster state, position eligibility, roster legality, and undo/correction.
- An LLM never determines roster legality, pick ownership, or whether a player has already been drafted. Persisted deterministic state wins every disagreement.
- Persist every selection in the same request that accepts it. Database constraints must prevent duplicate overall picks and duplicate drafted players.
- Preserve undo and correction behavior. Changes to pick mutation code require persistence and rollback tests.
- Reconstruct rosters from saved picks; do not maintain an independently mutable roster copy that can drift from draft history.
- Snapshot validated league configuration into a draft so an application restart reproduces identical in-progress state.
- Snapshot external import-run references into each draft. Never silently revalue an in-progress or archived draft against newer provider data.
- Draft reset is archival plus an empty cloned session. Destructive deletion is not the normal reset workflow.
- Keep quantitative player evaluation separate from both draft rules and LLM strategy. Quantitative evaluation occurs before LLM reasoning.
- Treat AI recommendations as advisory and disposable. The live board and manual entry must work when models, APIs, providers, or the internet are unavailable.
- Wrap external/model advisors with the resilient fallback boundary; never let an advisor exception escape into draft entry or board rendering.
- Provider integrations must map external records to canonical player IDs. Do not couple the draft engine to a ranking, ADP, projection, news, or model provider.
- Internal player IDs belong to this application. Provider IDs and player names are never canonical primary keys. Prefer exact external-ID matching; quarantine ambiguous fallback matches.
- Calculate fantasy points from raw provider statistics through the snapshotted league scoring configuration. Provider generic fantasy points are informational only.
- Preserve provider import audits, data-mode/freshness labels, source checksums/caches, and unmatched-review records. External failure must leave the last usable local data intact.
- Validate every AI response against a strict schema and the deterministic available-candidate allowlist. Use bounded timeouts, avoid page-load API spam, and persist only validated advisory history.
- Keep the short live-draft AI timeout separate from the longer readiness diagnostic timeout. Diagnostics must bypass live-result caches, report credential-safe latency/failure details, and never weaken immediate deterministic fallback.
- Export and backup formats must never include secrets. Readiness checks must not mutate the selected draft.
- Prefer small, testable deterministic functions and a single-process architecture. Avoid distributed infrastructure unless measured requirements justify it.

## Live-draft UX rules

- Optimize common pick entry for speed, clear confirmation, and visible current-team context.
- Keep current pick, our next pick, available players, recommendations, board, and rosters readable under time pressure.
- Make destructive/corrective actions explicit and easy to verify.
- Preserve manual fallbacks for all external-data and AI features.

## Change discipline

- Add or update tests for snake boundaries, configuration variants, roster eligibility, availability, persistence, restart recovery, undo, and correction whenever those areas change.
- Keep domain rules out of HTTP handlers and templates.
- Avoid unnecessary framework and infrastructure complexity. Prefer FastAPI, server-rendered HTML, SQLite, and small vanilla JavaScript until requirements prove they are insufficient.
- Do not silently reinterpret an active draft after YAML changes. New configuration applies to new drafts; active drafts use their snapshot.
- Sample player data is illustrative, not a trusted live ranking source. Identify provider and data vintage before using imported data for real recommendations.

## Security

- Never commit secrets, credentials, API keys, SSH keys, tokens, private datasets, or `.env` contents.
- Keep local databases and generated runtime files ignored.
- Use credentials only for the operation for which they were supplied, and never print or copy private-key contents.
