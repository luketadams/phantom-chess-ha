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
pip install pytest-homeassistant-custom-component
pytest custom_components/phantom_chess/tests/
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
