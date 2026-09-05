"""Hardware-free native contracts. Requires Swift CLI, not Xcode/XCTest."""
import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class NativeTests(unittest.TestCase):
    def test_native_contracts_and_process_lifecycle(self):
        subprocess.run(["swift", "build", "--target", "EInkCore"], cwd=ROOT, check=True, capture_output=True)
        native = ROOT / "Sources/EInkCompanion"
        with tempfile.TemporaryDirectory(prefix="eink-native-tests-") as temporary:
            executable = Path(temporary) / "native-tests"
            sources = [native / name for name in ("CompanionContracts.swift", "ProcessHost.swift", "NativeBLEController.swift", "CompanionModel.swift", "SendPreflight.swift")]
            tests = sorted((ROOT / "Tests/CompanionNativeTests").glob("*.swift"))
            objects = sorted((ROOT / ".build/debug/EInkCore.build").glob("*.swift.o"))
            built = subprocess.run(["swiftc", "-swift-version", "6", "-parse-as-library", "-I", str(ROOT / ".build/debug/Modules"),
                                    *map(str, sources + tests + objects), "-o", str(executable)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            isolated_home = Path(temporary) / 'home'
            isolated_home.mkdir()
            environment = dict(os.environ, COMPANION_TEST_PYTHON=sys.executable,
                               CFFIXED_USER_HOME=str(isolated_home))
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60, env=environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS 52 native tests", result.stdout)
            print(result.stdout)


if __name__ == "__main__":
    unittest.main()
