# Fantasy Draft AI

Fantasy Draft AI is a reliable, offline-capable live fantasy-football draft copilot. The first milestone tracks a manually entered physical draft board, calculates snake order and roster state deterministically, and provides fast advisory recommendations without allowing AI to control draft state.

## What works now

- Validated YAML league configuration with no league rules in application code
- Deterministic snake order, current pick, our next pick, and intervening-team calculation
- SQLite persistence with one transaction per selection
- Duplicate-player and duplicate-pick protection at both service and database levels
- Available-player tracking and all-team rosters derived from saved picks
- Config-driven roster legality, flex eligibility, and remaining starter needs
- Immediate undo and correction of any completed pick
- Fast server-rendered draft board, search/filter/sort controls, and draft buttons
- JSON API for state, pick entry, correction, and undo
- Replaceable player-evaluation and strategic-advisor interfaces
- Offline baseline recommendations that include estimated next-pick availability

The included `data/players.csv` is illustrative sample data, not a current draft ranking. Replace or import an authoritative dataset before a real draft.

## Local setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/fantasy-draft --reload
```

Open <http://127.0.0.1:8000>. The default database is created at `instance/fantasy_draft.db` and is ignored by Git. Stop `--reload` for the actual draft to reduce moving parts:

```bash
.venv/bin/fantasy-draft
```

If the OS Python lacks `venv`, install its standard venv package first (for example, `python3-venv` on Debian/Ubuntu).

## Configure the league

Edit `config/league.yaml` before creating the real draft. It captures the current settings from `league-settings.txt`, with documented temporary assumptions:

- eight teams named `Team 1` through `Team 8`;
- our team at draft slot 1;
- snake order;
- 17 drafted rounds because IR is not a draftable roster slot;
- `W/R` represented as a flex accepting WR or RB.

Update team names, `draft.our_team_id`, and each `draft_slot` when the order is known. Team IDs should remain stable once a draft begins. Roster and scoring rules can be changed in YAML without Python changes.

Each draft stores its validated configuration snapshot. This intentionally means editing YAML does not mutate an in-progress draft. During development, remove the ignored local database before starting a fresh draft with changed configuration:

```bash
rm instance/fantasy_draft.db
```

Only run that command when discarding the local draft is intentional.

Environment variables can point to other files or databases; copy `.env.example` values into your shell or process manager as needed. The application does not automatically read `.env` files.

## Player data

The provider-neutral CSV columns are shown in `data/players.csv`. `id` is the stable canonical application identity. `provider` and `provider_id` preserve one external mapping; the database model supports a larger external-ID map for future provider matching.

Import or upsert another CSV into the configured database:

```bash
.venv/bin/fantasy-draft-import path/to/players.csv
```

All quantitative fields are optional. A future provider adapter can populate projections, VOR, scarcity, richer floors/ceilings, and survival models without changing draft rules.

## API

- `GET /api/state` — deterministic draft state plus advisory recommendations
- `POST /api/picks` with `{"player_id": "..."}` — save the current pick
- `PUT /api/picks/{overall}` with `{"player_id": "..."}` — correct a pick
- `DELETE /api/picks/last` — undo the last pick
- `GET /health` — database health check

Interactive API documentation is at <http://127.0.0.1:8000/docs>.

## Tests

```bash
.venv/bin/pytest
```

Tests cover snake direction and boundaries, current/next-pick calculations, config validation and variation, roster eligibility, availability, duplicate prevention, transaction rollback, undo/correction, restart reconstruction, and browser/API flow.

## Architecture

`fantasy_draft/engine.py` is pure deterministic domain logic. `services.py` coordinates transactions and state reconstruction. SQLAlchemy models in `models.py` persist canonical players, draft teams, configuration snapshots, and picks. `evaluation.py` defines provider/advisor boundaries, a failure-isolating advisor wrapper, and an offline fallback. `web.py` is a thin HTTP/UI adapter.

Future LLM integration should consume structured deterministic state and evaluated candidates, return concise advice, use strict timeouts, and fail open to the existing board. It must never write or reinterpret draft state.
