# Build and set up Codex Ink

[简体中文](installation.zh-CN.md) · [Project overview](../README.md)

This is a developer preview built from source. It requires external Python/Pillow. A notarized DMG and a verified second-Mac installation are not available yet.

## 1. Prepare the environment

Use a Swift 6 toolchain. The macOS deployment target is 14; the tested machine is Apple Silicon on macOS 26.6.2 with Swift 6.3.3. From the checkout root:

```bash
git clone https://github.com/hubinvip-ai/codex-ink.git
cd codex-ink
swift --version
xcode-select -p
python3 --version
```

The selected Python interpreter needs Pillow. Tested versions: Python 3.9.6 and Pillow 11.3.0. To prepare an isolated environment with an installed compatible Python:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'from PIL import Image; print(Image.__version__)'
```

Select that interpreter in the app. Keep it at a stable location: moving the checkout also moves `.venv`. The build script does not install these packages.

Keep Codex signed in. Its executable must provide the account, quota, task, usage-history and hook-catalog methods used by this project. Availability depends on the installed version; a failed preview does not mean zero usage.

## 2. Build and place the app

```bash
python3 tools/build_companion.py
```

The command prints the generated `Codex Ink.app` path. It packages the executable, Python scripts, font and font license with an ad-hoc development signature. It does not launch the app, install hooks or enable login startup.

Using Finder, move the app to a stable location such as `~/Applications/Codex Ink.app` before setting up hooks. Do not install hooks from the temporary build directory. Read the update notes before replacing an existing copy.

Ad-hoc signing is not Developer ID signing or Apple notarization. Other Macs may require additional review or authorization; the distribution experience is unverified. Do not disable system security protections to bypass an error.

## 3. Preview and bind

Choose Simplified Chinese or English in Overview → 语言 / Language. The choice applies to settings, the menu bar and the display. Existing configurations remain in Chinese.

1. Open settings, choose the actual Codex and Python executables and click **保存路径** (Save paths).
2. Click **检查数据并生成预览** (Check data and generate preview).
3. Click **扫描设备** (Scan), review macOS Bluetooth authorization and select a display.
4. Wait for GATT validation and disconnect confirmation before accepting the binding.

The target is the documented 400 × 300 black/white/red BLE protocol. A device name alone cannot establish compatibility; a preview is not a successful physical update.

## 4. Install hooks and enable sync

1. Use **检查接管状态** (Check setup) to inspect existing hooks and services.
2. When ready to interrupt your Codex work, close its sessions yourself. Use **安装 / 接管…** (Install / take over) and review the confirmation.
3. Reopen Codex and review/trust the installed hooks through Codex's own controls.
4. Trigger a normal task event. Setup requires a valid receipt and trusted hook definitions before sync is enabled.
5. Enable synchronization and check the physical display.

Unrelated hooks are preserved. Unknown sources or another sender block setup. Do not clear your Codex configuration to bypass a blocked setup.

## 5. Login startup

After saving valid configuration, enable **登录时打开** (Open at login). The app saves the selected state directory to `~/Library/Application Support/CodexEInk/startup.json` before registering with macOS. Normal launches then restore that configuration without extra arguments.

Disabling login startup preserves the directory choice and does not trigger re-registration. Invalid startup records or missing settings stop recovery with an error. No-argument restart and automatic BLE transmission have been checked locally; a true logout/login cycle is still pending.

## Updating an existing install

Build the new version in a separate output directory, then run the generated `更新 Codex Ink.command`. It targets `~/Applications/Codex Ink.app`, waits for active transmission to settle, preserves the trusted `runtime`, updates `sync-runtime`, and restarts the app. It retains a rollback copy and validates startup for up to 90 seconds. Ordinary updates do not require quitting Codex or trusting unchanged hooks again.

For a different installed path, use the updater's explicit `--target-app` option. An existing setup must pass inspection. First-time setup and intentional changes to hook paths or content still require the controlled setup/trust workflow. Keep older runtime directories referenced by existing hooks. Settings, binding and login-startup records are preserved.

Avoid multiple app, CLI or phone controllers sending to the same display. See the [operator guide](companion-operator.md).

## Troubleshooting

| Symptom | Next action |
|---|---|
| Missing PIL | Check Pillow with the exact interpreter selected in settings |
| Preview cannot read data | Check Codex sign-in, executable path and required capabilities |
| Setup blocked | Follow the specific source/process/trust error and controlled setup flow |
| Display not found | Check power, permissions, protocol and other connected controllers |
| startup.json error | Repair that record and its referenced settings; no replacement is generated |

Include versions, the failing step and a redacted error in a report. Read [privacy notes](PRIVACY.md) before sharing diagnostics.
