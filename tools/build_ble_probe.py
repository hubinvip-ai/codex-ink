#!/usr/bin/env python3
"""Build an isolated, ad-hoc-signed experiment. Never launch or install it."""
import argparse
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "EInkBLEProbe.app"
EXECUTABLE = "eink-ble-probe"


def experiment_directory(output=None, temp_root=None):
    if output is not None:
        destination = Path(output).expanduser().absolute()
        destination.mkdir(parents=True, exist_ok=False)
        return destination
    return Path(tempfile.mkdtemp(prefix="eink-ble-probe-", dir=temp_root))


def package_app(executable, experiment):
    executable, experiment = Path(executable), Path(experiment)
    if not executable.is_file():
        raise FileNotFoundError(executable)
    app = experiment / APP_NAME
    app.mkdir(exist_ok=False)
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / EXECUTABLE
    shutil.copyfile(executable, binary)
    binary.chmod(0o755)
    with (ROOT / "config/ble-probe-Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    with (contents / "Info.plist").open("xb") as stream:
        plistlib.dump(info, stream)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="New experiment directory; must not exist. Default: unique system temporary directory.")
    args = parser.parse_args()
    experiment = experiment_directory(output=args.output_dir)
    scratch = experiment / "swift-build"
    common = ["swift", "build", "--package-path", str(ROOT), "--scratch-path", str(scratch), "-c", "release"]
    subprocess.run(common + ["--product", EXECUTABLE], check=True)
    binary_dir = Path(subprocess.check_output(common + ["--show-bin-path"], text=True).strip())
    app = package_app(binary_dir / EXECUTABLE, experiment)
    report = experiment / "probe-report.json"
    print(json.dumps({
        "app": str(app), "executable": str(app / "Contents/MacOS" / EXECUTABLE),
        "report": str(report), "report_created": False,
        "signing_context": "ad-hoc-development", "launched": False,
        "launch_command": shlex.join(["open", "-n", str(app), "--args", "--report", str(report), "--device", "NRF_325608"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
