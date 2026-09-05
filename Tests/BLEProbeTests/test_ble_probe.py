"""Hardware-free packaging and report/state-machine contract tests.

Run: python3 -B -m unittest discover -s Tests/BLEProbeTests -v
Only the Foundation test harness is executed, never the probe app.
"""
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PackagingTests(unittest.TestCase):
    def test_swift_package_exposes_only_independent_probe_target(self):
        result = subprocess.run(["swift", "package", "dump-package"], cwd=ROOT, check=True, capture_output=True, text=True)
        package = json.loads(result.stdout)
        targets = {target["name"]: target for target in package["targets"]}
        self.assertIn("EInkBLEProbe", targets, "independent executable target is missing")
        self.assertEqual(targets["EInkBLEProbe"]["dependencies"], [])
        product = next(p for p in package["products"] if p["name"] == "eink-ble-probe")
        self.assertEqual(product["targets"], ["EInkBLEProbe"])

    def builder(self):
        path = ROOT / "tools/build_ble_probe.py"
        self.assertTrue(path.is_file(), "independent probe builder is not implemented")
        spec = importlib.util.spec_from_file_location("build_ble_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_packages_standalone_signed_app_without_installed_dependencies(self):
        builder = self.builder()
        with tempfile.TemporaryDirectory(prefix="ble-probe-package-test-") as directory:
            # A real Mach-O fixture exercises copying, plist generation and signing,
            # without compiling or executing the GUI or touching Bluetooth.
            app = builder.package_app(Path("/usr/bin/true"), Path(directory))
            with (app / "Contents/Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            self.assertEqual(info["CFBundleExecutable"], "eink-ble-probe")
            self.assertEqual(info["CFBundleIdentifier"], "com.ben.codex-eink.ble-probe.dev")
            self.assertEqual(info["CFBundlePackageType"], "APPL")
            self.assertEqual(info["LSMinimumSystemVersion"], "14.0")
            self.assertTrue(info["NSBluetoothAlwaysUsageDescription"])
            self.assertEqual(info["BLEProbeSigningContext"], "ad-hoc-development")
            executable = app / "Contents/MacOS/eink-ble-probe"
            self.assertTrue(executable.is_file())
            self.assertTrue(executable.stat().st_mode & 0o111)
            self.assertFalse(any(p.is_symlink() for p in app.rglob("*")))
            subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True, capture_output=True)
            signing = subprocess.run(["codesign", "-d", "-vv", str(app)], check=True, capture_output=True, text=True)
            self.assertIn("Signature=adhoc", signing.stderr)
            self.assertNotIn("/Library/", plistlib.dumps(info).decode())

    def test_existing_bundle_is_never_overwritten(self):
        builder = self.builder()
        with tempfile.TemporaryDirectory(prefix="ble-probe-package-test-") as directory:
            app = builder.package_app(Path("/usr/bin/true"), Path(directory))
            before = (app / "Contents/MacOS/eink-ble-probe").read_bytes()
            with self.assertRaises(FileExistsError):
                builder.package_app(Path("/usr/bin/true"), Path(directory))
            self.assertEqual((app / "Contents/MacOS/eink-ble-probe").read_bytes(), before)

    def test_default_experiment_directories_are_unique_and_explicit_path_is_exclusive(self):
        builder = self.builder()
        with tempfile.TemporaryDirectory(prefix="ble-probe-path-test-") as directory:
            root = Path(directory)
            a = builder.experiment_directory(temp_root=root)
            b = builder.experiment_directory(temp_root=root)
            self.assertNotEqual(a, b)
            self.assertEqual(a.parent, root)
            self.assertTrue(a.is_dir())
            with self.assertRaises(FileExistsError):
                builder.experiment_directory(output=a)


class ReportContractTests(unittest.TestCase):
    def test_swift_report_and_state_machine_without_bluetooth(self):
        source = ROOT / "Sources/EInkBLEProbe/ProbeContract.swift"
        self.assertTrue(source.is_file(), "probe report/state machine is not implemented")
        with tempfile.TemporaryDirectory(prefix="ble-probe-contract-test-") as directory:
            executable = Path(directory) / "probe-contract-tests"
            built = subprocess.run([
                "swiftc", "-swift-version", "6", str(source),
                str(ROOT / "Tests/BLEProbeTests/main.swift"), "-o", str(executable),
            ], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            for scenario in ("cancel", "timeout", "connected-cancel"):
                with self.subTest(scenario=scenario):
                    interleaving = subprocess.run([str(executable), directory, scenario], capture_output=True, text=True)
                    self.assertEqual(interleaving.returncode, 0, interleaving.stdout + interleaving.stderr)
                    self.assertIn("PASS cleanup interleaving " + scenario, interleaving.stdout)
            result = subprocess.run([str(executable), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS all probe contracts", result.stdout)
            report = json.loads((Path(directory) / "evidence.json").read_text())
            for stage in ("authorization", "manager_state", "discovered", "connected", "service_matched", "characteristic_matched", "disconnected"):
                self.assertEqual(report[stage], "pass")
            self.assertIsNone(report["error_code"])
            self.assertEqual(report["phase"], "finished")


if __name__ == "__main__":
    unittest.main()
