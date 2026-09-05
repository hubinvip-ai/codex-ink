# Contributing to Codex Ink

This is a development preview licensed under [GPL-3.0-only](LICENSE). Contributions to project code are made under the same license. Preserve third-party notices and document any new source reuse; read [third-party notices](THIRD_PARTY_NOTICES.md).

## Architecture

- `Sources/EInkCompanion/`: macOS UI, configuration, process lifecycle and native BLE ownership.
- `Sources/EInkCore/`: native frame and BLE packet encoding.
- `tools/`: local Codex data access, hooks, persistent refresh queue and rendering.
- `Tests/`: Python and native contract/process checks.

Use [build instructions](docs/installation.md) to prepare the local environment. Keep changes focused, preserve existing file/protocol contracts, and document changes before altering them.

## Full hardware-free check

After installing `requirements.txt` into `.venv`:

```bash
.venv/bin/python scripts/run_tests.py
swift build -c release
swift run eink-core-tests
```

## Relevant checks

Run only the suites affected by your change. For companion configuration or startup changes:

```bash
python3 -m unittest discover -s Tests/CompanionNativeTests -v
python3 -m unittest discover -s Tests/CompanionPackageTests -v
swift build -c release --product eink-companion
git diff --check
```

For renderer/layout changes, use `Tests/TextRendererTests` and `Tests/HtmlLayoutTests`; for setup migration use `Tests/CompanionSetupTests`; for bridge/data changes use `Tests/CompanionBridgeTests`. Native tests include process/pipe checks and mock BLE; they do not establish physical-device success.

## Device UI

The 400 × 300 device layout is frozen at v1.2. Read [the UI specification](docs/eink-ui-spec.md) before changing it. Do not overwrite historical baseline assets. A visual change needs a new approved specification, matching HTML/native implementation and a new baseline.

## Pull requests and reports

Explain the problem, final behavior and validation. Separate automated tests, local app checks, BLE transmission and physical appearance. Do not use production account data or real task names in test fixtures or screenshots.

Share small, redacted reproductions. Do not include personal runtime state, credentials or trust records. Never modify hook trust to bypass a blocked setup.
