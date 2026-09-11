import importlib.util
import hashlib
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


class PackageTests(unittest.TestCase):
    def builder(self):
        script=ROOT/'tools/build_companion.py'
        self.assertTrue(script.exists(),'companion builder missing')
        spec=importlib.util.spec_from_file_location('build_companion',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_initial_runtimes_are_identical_and_individually_checksummed(self):
        module=self.builder()
        with tempfile.TemporaryDirectory() as temp:
            app=module.package_app(Path('/usr/bin/true'),Path(temp))
            resources=app/'Contents/Resources'
            self.assertTrue((resources/'sync-runtime').is_dir())
            inventories=[]
            for name in ('runtime','sync-runtime'):
                runtime=resources/name
                inventory={str(p.relative_to(runtime)):p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
                manifest=json.loads(inventory.pop('manifest.json'))
                self.assertEqual(manifest['version'],1)
                self.assertEqual(manifest['files'],{name:hashlib.sha256(data).hexdigest() for name,data in inventory.items()})
                self.assertFalse(any('__pycache__' in name for name in inventory))
                inventories.append(inventory)
            self.assertEqual(inventories[0],inventories[1])

    def test_package_marks_update_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            app=self.builder().package_app(Path('/usr/bin/true'),Path(temp))
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            self.assertEqual(info.get('CodexInkUpdateProtocol'),1)
            self.assertIs(type(info['CodexInkUpdateProtocol']),int)

    def test_update_launcher_handles_special_directory_characters(self):
        with tempfile.TemporaryDirectory(prefix="Codex ' 中文 $ ") as temp:
            app=self.builder().package_app(Path('/usr/bin/true'),Path(temp))
            launcher=Path(temp)/'更新 Codex Ink.command'
            self.assertTrue(launcher.is_file())
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertIn('exec /usr/bin/python3',launcher.read_text())
            # Substitute only the packaged entry point; never execute a real update.
            updater=app/'Contents/Resources/sync-runtime/tools/update_companion.py'
            updater.write_text('import json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            result=subprocess.run([str(launcher)],cwd='/',capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout),['--source-app',str(app)])

    def test_package_is_self_contained_except_explicit_python(self):
        module=self.builder()
        with tempfile.TemporaryDirectory() as temp:
            app=module.package_app(Path('/usr/bin/true'),Path(temp))
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleIdentifier'],'com.ben.codex-eink.companion.dev')
            self.assertTrue(info['LSUIElement'])
            self.assertTrue(info['NSBluetoothAlwaysUsageDescription'])
            self.assertEqual(info['CFBundleExecutable'],'eink-companion')
            self.assertEqual(info['CFBundleIconFile'],'CodexInk')
            resources=app/'Contents/Resources'
            self.assertEqual((resources/'CodexInk.icns').read_bytes(),(ROOT/'assets/icons/CodexInk.icns').read_bytes())
            for name in ('CodexInkMenuBarTemplate.png','CodexInkMenuBarTemplate@2x.png'):
                self.assertEqual((resources/name).read_bytes(),(ROOT/'assets/icons/menu-bar'/name).read_bytes())
            self.assertEqual((resources/'Licenses/LICENSE').read_bytes(),(ROOT/'LICENSE').read_bytes())
            self.assertEqual((resources/'Licenses/THIRD_PARTY_NOTICES.md').read_bytes(),(ROOT/'THIRD_PARTY_NOTICES.md').read_bytes())
            runtime=app/'Contents/Resources/runtime'
            for name in ('companion_bridge.py','companion_data.py','companion_setup.py','codex_eink_reliable.py','eink_text_renderer.py'):
                self.assertTrue((runtime/'tools'/name).is_file())
            self.assertTrue((runtime/'assets/fonts/SourceHanSansCN-Bold.otf').is_file())
            self.assertTrue((runtime/'assets/fonts/LICENSE-SourceHanSans.txt').is_file())
            self.assertFalse(any(p.is_symlink() for p in app.rglob('*')))
            subprocess.run(['codesign','--verify','--strict',str(app)],check=True,capture_output=True)
            result=subprocess.run(['/usr/bin/python3',str(runtime/'tools/companion_setup.py'),'--state-dir',str(Path(temp)/'state'),'inspect'],capture_output=True,text=True)
            self.assertIn('stage',json.loads(result.stdout))

    def test_existing_app_is_preserved(self):
        module=self.builder()
        with tempfile.TemporaryDirectory() as temp:
            app=module.package_app(Path('/usr/bin/true'),Path(temp))
            original=(app/'Contents/Info.plist').read_bytes()
            with self.assertRaises(FileExistsError):module.package_app(Path('/usr/bin/true'),Path(temp))
            self.assertEqual((app/'Contents/Info.plist').read_bytes(),original)


if __name__=='__main__':unittest.main()
