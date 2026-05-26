# Contributing to Phantom Chess Board

Issues, pull requests, and protocol notes are welcome. The integration is a community project — there's no formal review SLA, but every issue gets read.

## Filing an issue

For bug reports, include:

- **Integration version** (manifest.json or HACS page)
- **Home Assistant Core version** (Settings → System → Repairs → ⋮ → System Information)
- **Hardware**: HA host arch (amd64 / aarch64), board firmware version (shown in the integration's diagnostics download)
- **Log excerpt** covering the timeframe of the issue. Set `logger.set_level` on `custom_components.phantom_chess` to `debug` first to capture the relevant detail.
- **Diagnostics dump**: click *Download Diagnostics* on the integration's device page. Sensitive fields are auto-redacted (token, partial MAC, username). Attach the JSON.

For protocol or firmware issues — those are out of scope here; please file them with Phantom directly.

## Development setup

```bash
git clone https://github.com/luketadams/phantom-chess-ha
cd phantom-chess-ha
pip install pre-commit pytest pytest-homeassistant-custom-component
pre-commit install
```

Run the test suite:

```bash
# Pure-function tests (fast, no HA scaffolding needed)
pytest custom_components/phantom_chess/tests/test_matrix.py

# Full HA-integration tests
pytest custom_components/phantom_chess/tests/
```

## Code style

- Type hints encouraged on public methods. Internal helpers — best-effort, but the existing coordinator has spotty hints (in progress).
- Match existing module structure rather than introducing new patterns.
- Public-facing strings (notifications, log messages users will see) go through HA's translation system when possible.
- Never log sensitive data — Lichess tokens, full MAC addresses, BLE auth state.
- For BLE writes, prefer the retry-aware `_ble_write` helper over direct `bleak` calls.

## Architecture notes

- **`matrix.py`** — pure functions for FEN ↔ matrix conversion, sensor diffing. No HA dependencies. Easy to test in isolation.
- **`lichess_analysis.py`** — async wrapper around Lichess cloud-eval, opening explorer, and the bundled Stockfish fallback. Architecture detection (libc + machine) lives here.
- **`coordinator.py`** — currently a god-object hosting BLE protocol, Lichess streaming, analysis hooks, TTS, notifications, dashboard state, services. Marked for split.
- **`config_flow.py`** — Bluetooth discovery, manual entry, Lichess token validation. Includes the reauth flow and options flow.
- **`diagnostics.py`** — HA Diagnostics platform; produces redacted debug dumps for issue reports.

## Pull request checklist

- [ ] Tests added or updated for new behavior
- [ ] CHANGELOG.md updated under `[Unreleased]`
- [ ] README and/or service descriptions updated if user-facing
- [ ] No regressions in `pytest tests/`
- [ ] No new hardcoded entity IDs, MAC addresses, or `/config/*` paths

## License

By contributing you agree your code may be released under the MIT license, same as the rest of the project.
