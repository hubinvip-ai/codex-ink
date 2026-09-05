# Historical CLI and layout tools

These tools remain for protocol work, rendering experiments and older installations. The supported development-preview entry point is the [Codex Ink companion app](installation.md).

## Editable HTML

Open `editable-dashboard.html` or serve the checkout locally. It is an editable sample, not a live Codex status page. Historical samples may contain private fixture values and must be reviewed before public distribution. Do not use its screenshot as proof of a live device update.

For a publication-safe example rendered with the current native profile, use:

```bash
python3 docs/release-assets/render_demo.py
```

This writes only the documented demo asset and makes no Codex or BLE calls.

## Encoding without a device connection

```bash
swift run eink-push \
  --input docs/release-assets/dashboard-demo.png \
  --quantized-output /tmp/codex-ink-demo-quantized.png \
  --prepare-only
```

`--prepare-only` validates and prepares the frame without transmitting it.

## Older sync entry points

`tools/codex_eink_sync.py`, the legacy LaunchAgent template and `config/hooks.json` describe the earlier polling/hook sender architecture. They are not installation instructions for new companion users. Templates include machine-specific legacy paths and need review before publication.

Do not run the old sender alongside the companion against the same device. See [reliable sync operations](reliable-sync-operator.md), [BLE protocol notes](ble-protocol.md) and [companion migration guidance](companion-operator.md) for development context.
