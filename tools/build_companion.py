#!/usr/bin/env python3
"""Build a development app with scripts/fonts, without installing or enabling it."""

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def package_app(executable, destination):
    executable,destination=Path(executable),Path(destination)
    if not executable.is_file():raise FileNotFoundError(executable)
    app=destination/'Codex Ink.app'
    app.mkdir(exist_ok=False)
    macos=app/'Contents/MacOS';macos.mkdir(parents=True)
    binary=macos/'eink-companion'
    shutil.copyfile(executable,binary);binary.chmod(0o755)
    with (ROOT/'config/companion-Info.plist').open('rb') as stream:info=plistlib.load(stream)
    info['CodexInkUpdateProtocol']=1
    with (app/'Contents/Info.plist').open('xb') as stream:plistlib.dump(info,stream)
    resources=app/'Contents/Resources';resources.mkdir(parents=True)
    licenses=resources/'Licenses';licenses.mkdir()
    for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(ROOT/name,licenses/name)
    (licenses/'SOURCE.md').write_text('Codex Ink source and full notices: https://github.com/hubinvip-ai/codex-ink\nProject license: GPL-3.0-only. See LICENSE. Font terms remain SIL OFL 1.1.\n')
    shutil.copyfile(ROOT/'assets/icons/CodexInk.icns',resources/'CodexInk.icns')
    for name in ('CodexInkMenuBarTemplate.png','CodexInkMenuBarTemplate@2x.png'):
        shutil.copyfile(ROOT/'assets/icons/menu-bar'/name,resources/name)
    runtime=resources/'runtime'
    (runtime/'tools').mkdir(parents=True)
    for source in sorted((ROOT/'tools').glob('*.py')):
        if not source.name.startswith('build_'):
            shutil.copyfile(source,runtime/'tools'/source.name)
    fonts=runtime/'assets/fonts';fonts.mkdir(parents=True)
    for name in ('SourceHanSansCN-Bold.otf','LICENSE-SourceHanSans.txt'):
        shutil.copyfile(ROOT/'assets/fonts'/name,fonts/name)
    for name in ('companion_bridge.py','companion_data.py','companion_setup.py'):
        if not (runtime/'tools'/name).is_file():raise FileNotFoundError(name)
    hashes={str(p.relative_to(runtime)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(runtime.rglob('*')) if p.is_file()}
    (runtime/'manifest.json').write_text(json.dumps({'version':1,'files':hashes},indent=2)+'\n')
    # New installs start with identical runtimes. Updates preserve runtime and
    # replace sync-runtime independently so existing trusted hooks stay valid.
    shutil.copytree(runtime,resources/'sync-runtime')
    subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
    subprocess.run(['codesign','--verify','--strict',str(app)],check=True)
    launcher=destination/'更新 Codex Ink.command'
    launcher.write_text('#!/bin/sh\nset -eu\n'
                        'dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                        'exec /usr/bin/python3 "$dir/Codex Ink.app/Contents/Resources/sync-runtime/tools/update_companion.py" --source-app "$dir/Codex Ink.app"\n')
    launcher.chmod(0o755)
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if args.output_dir:
        directory=args.output_dir.expanduser().resolve();directory.mkdir(parents=True,exist_ok=False)
    else:directory=Path(tempfile.mkdtemp(prefix='eink-companion-'))
    common=['swift','build','--package-path',str(ROOT),'--scratch-path',str(directory/'swift-build'),'-c','release']
    subprocess.run(common+['--product','eink-companion'],check=True)
    binary_dir=Path(subprocess.check_output(common+['--show-bin-path'],text=True).strip())
    app=package_app(binary_dir/'eink-companion',directory)
    print(json.dumps({'app':str(app),'runtime':str(app/'Contents/Resources/runtime'),
                     'sync_runtime':str(app/'Contents/Resources/sync-runtime'),
                     'updater':str(directory/'更新 Codex Ink.command'),
                     'signing':'ad-hoc-development','installed':False,'external_python_required':True},ensure_ascii=False))


if __name__=='__main__':main()
