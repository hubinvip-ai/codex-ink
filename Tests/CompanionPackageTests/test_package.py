import importlib.util
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
