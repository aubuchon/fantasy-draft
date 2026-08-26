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
- Store player birth date and draft year from audited provider/crosswalk fields; derive display age and experience rather than freezing time-dependent values in source code.
- Treat NFL team as mutable metadata, not player identity. A unique name/position fallback may survive a team change, but ambiguous identities must remain quarantined.
- Retire a fallback duplicate automatically only when it is sample-only and exactly one provider-ID-backed canonical identity exists. Preserve or safely repoint draft history; never silently merge competing authoritative identities.
- Exclude unresolved duplicate identities from deterministic and LLM recommendations, expose them in data health, and fail the readiness identity check until resolved.
- Calculate fantasy points from raw provider statistics through the snapshotted league scoring configuration. Provider generic fantasy points are informational only.
- Calculate replacement level against remaining league demand after subtracting drafted players; never apply full-league demand indices directly to an already-depleted available pool.
- Model diminishing marginal value at surplus roster positions and ensure every viable unfilled starter position appears in the advisor candidate set. Do not compare raw cross-position season totals as roster utility.
- Keep user strategy preferences explicit and snapshotted. Rookie and preferred-team bonuses are small tie-breakers; they never override legality, identity, required roster construction, or major value gaps.
- Treat configured user-team position maximums as deterministic pick constraints. Treat target rounds as soft, explainable evaluation preferences that retain an emergency candidate; never apply either preference to opponent roster legality.
- Preserve provider import audits, data-mode/freshness labels, source checksums/caches, and unmatched-review records. External failure must leave the last usable local data intact.
- Validate every AI response against a strict schema and the deterministic available-candidate allowlist. Use bounded timeouts, avoid page-load API spam, and persist only validated advisory history.
- Keep the short live-draft AI timeout separate from the longer readiness diagnostic timeout. Diagnostics must bypass live-result caches, report credential-safe latency/failure details, and never weaken immediate deterministic fallback.
- Export and backup formats must never include secrets. Readiness checks must not mutate the selected draft.
- Prefer small, testable deterministic functions and a single-process architecture. Avoid distributed infrastructure unless measured requirements justify it.

## Live-draft UX rules

- Optimize common pick entry for speed and visible current-team context. Draft buttons save in one click without a confirmation dialog; prevent accidental double submission and rely on the prominent undo/correction workflow for mistakes.
- Version browser assets and use pick-form hooks that stale JavaScript cannot mistake for older confirmation workflows.
- Keep current pick, our next pick, available players, recommendations, board, and rosters readable under time pressure.
- Make destructive/corrective actions explicit and easy to verify.
- Preserve manual fallbacks for all external-data and AI features.
- Automatically call the live advisor only after the preceding pick puts our team on the clock. Cache success and safe failure markers for unchanged state; repeat requests require an explicit user retry.
- Persist the user-selected live model and make it authoritative for the next automatic request. Allow explicit manual recalculation before our turn; model-specific fingerprints must keep one model's cached result or failure from suppressing another model.
- Give the LLM a curated, roster-aware candidate set and every team roster. Deterministic code evaluates the full available pool; do not flood the model with the entire player database or starve it of required-position candidates.
- Include the draft's complete snapshotted scoring and roster rules in every advisor packet. Keep the candidate budget configurable and position-diverse; increasing the outer limit must also provide useful per-position depth.

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
