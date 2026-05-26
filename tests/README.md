# Phantom Chess — test suite

Initial test coverage. Currently a small but representative set; will grow.

## Running

### Pure-function tests (no HA scaffolding needed)

```bash
pip install pytest
cd custom_components/phantom_chess
PYTHONPATH=. pytest tests/test_matrix.py -v
```

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

- `test_matrix.py` — pure-function tests covering FEN ↔ matrix conversion,
  sensor consistency checks, mismatch diffing, and piece-name mapping.
  Runs in milliseconds, no HA scaffolding.
- `test_config_flow.py` — Bluetooth discovery, token validation, reauth,
  options flow. Requires the full HA test plugin.
- `conftest.py` — shared fixtures (mock Lichess responses, mock BLE device).

## Adding tests

Follow the layout above:
- Pure functions → `test_<module>.py` next to the source module.
- HA-integration → `test_<surface>.py` with proper `hass` fixture.

Aim to keep pure-function tests independent of HA imports so they
remain fast and self-contained.
