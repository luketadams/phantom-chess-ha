
<!-- phantom-reference-navigation -->
> **Engineering navigation.** Cross-document baseline, evidence, and unresolved defects are coordinated centrally. Current decisions and conflict resolutions: [engineering reference](../docs/ENGINEERING_REFERENCE.md).

# Phantom Chess — test suite

Test suite spanning two CI jobs: a fast minimal-env `matrix-tests` group (no
Home Assistant installed) and a full `ha-tests` group via
`pytest-homeassistant-custom-component`. See the Layout section for the split.

## Running

### Pure-function tests (no HA scaffolding needed)

```bash
pip install pytest pytest-asyncio
PYTHONPATH=. pytest tests/test_matrix.py -v
```

`pytest-asyncio` is required because the minimal-env suite now includes
async tests (e.g. `test_ai_vs_ai_resilience.py`); `asyncio_mode = "auto"`
(in `pyproject.toml`) only takes effect when the plugin is installed. This
mirrors the CI `matrix-tests` job — any new `async def` test added to that
job's file list needs `pytest-asyncio` present or it errors with "async def
functions are not natively supported."

Or, since the matrix module is a pure standalone, even simpler:

```bash
python3 -c "
import sys; sys.path.insert(0, 'custom_components/phantom_chess')
import matrix
m = matrix.build_matrix_from_fen('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR')
assert matrix.grid_to_fen(m) == 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR'
print('matrix.py round-trip OK')
"
```

### Full HA-integration tests (config flow, coordinator)

These require `pytest-homeassistant-custom-component`, which pulls in
the full HA dependency tree (voluptuous, aiohttp, etc.):

```bash
# Use Python 3.14 for the current HA baseline.
pip install homeassistant==2026.8.3 pytest-homeassistant-custom-component==0.13.357 \
  aiousbwatcher==1.1.2 serialx==1.8.2 'chess>=1.10,<2'
pytest tests/
```

The `hass` fixture from the plugin sets up an in-memory HA instance
the tests can drive.

## Layout

Two groups, matching the CI jobs:

**Minimal-env (CI `matrix-tests` job — no HA install, needs only
`pytest pytest-asyncio chess pyyaml aiohttp`):**

- `test_matrix.py` — FEN ↔ matrix conversion, sensor consistency, mismatch
  diffing, piece-name mapping.
- `test_dashboard_provision.py` — dashboard template rendering / sanitizer-safe
  markup.
- `test_lichess_analysis.py` — Lichess analysis client parse + cache + AI-level
  table.
- `test_coordinator_helpers.py`, `test_coordinator_state.py` — pure coordinator
  helpers and state transitions.
- `test_diagnostics.py` — diagnostics payload shape.
- `test_ai_vs_ai_resilience.py` — AI-vs-AI BLE-drop recovery loop (async;
  needs `pytest-asyncio`).

**Full HA-integration (CI `ha-tests` job — needs
`pytest-homeassistant-custom-component`):**

- `test_config_flow.py` — Bluetooth discovery, token validation, reauth,
  options flow. Gated by `pytest.importorskip` so it skips cleanly in the
  minimal env.

`conftest.py` — shared fixtures plus the minimal-env stub that stages
`matrix.py` / `dashboard_provision.py` into `sys.modules` with stubbed
`homeassistant.*` so the pure-function tests import naturally in both envs.

## Adding tests

Follow the layout above:
- Pure functions → `test_<module>.py` next to the source module.
- HA-integration → `test_<surface>.py` with proper `hass` fixture.

Aim to keep pure-function tests independent of HA imports so they
remain fast and self-contained.

## Reproducible local environment

Use an isolated environment to avoid missing async plugins or unrelated global
linters. The CI versions remain the reference:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python pytest==9.1.1 pytest-asyncio==1.3.0 chess pyyaml aiohttp ruff==0.15.20 mypy==2.1.0
.venv/bin/python -m pytest tests/ -q
```

The full HA job additionally needs the pinned packages in the test workflow under `.github/workflows/`. The
September 2026 reliability regression tests live in `test_local_reliability.py`.

## Current rebuild validation

Both full environments pass 1,562 tests at 94.23% line coverage: HA 2026.2.3 / Python 3.13 and HA 2026.8.3 / Python 3.14.7. CI pins both HA/fixture combinations. Local macOS tests do not qualify its Bluetooth hardware or Linux runtime.

`test_session_integrity.py` asserts correct behavior under concurrency, pause, failed starts, stale analysis, engine termination, undo/reset timeouts and competing physical operations. Hardware acceptance remains required. Durable outputs are linked from the engineering reference.

## Native frontend

Run `npm ci`, `npx playwright install chromium`, then `npm run test:frontend`. The test loads the actual packaged card in Chromium and checks entry routing, moves, busy-state gating, PGN service responses, HTML escaping, search input, coaching visibility, error messages and mobile overflow. Screenshots use sample state and are not live hardware evidence.

`scripts/build_release.py` creates deterministic ZIP/TAR artifacts and SHA-256 file manifests. The build remains a beta until the physical acceptance sequence is completed.


Game review is covered by `test_game_review.py`: mover-relative grading, legal PVs, terminal outcomes, cancellation before/during work, restart persistence, changed-line invalidation, storage failures, active-game preemption and library service routing. The browser harnesses have 54 assertions including review/practice navigation and mobile layout. Live release checks use a temporary imported game, never robotic moves.

Verified engine installation tests exercise streamed bounds, archive/executable hashes, exact regular-file selection, atomic replacement, cancellation, concurrent entries and process recovery. Real signed artifacts were separately verified as recorded in `docs/ENGINE_ARTIFACTS.md`.

`frontend-review-recovery.cjs` uses [Playwright’s clock](https://playwright.dev/docs/clock) to test temporary read failures, the five-attempt retry bound, manual refresh, late replies, card reconnection and stopping polling outside Review. These checks use the actual bundled card and mock only service responses.
