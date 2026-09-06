import inspect
import shutil
import tempfile
import unittest
from unittest.mock import patch
from tools import eink_text_renderer as renderer
from tools.sync_adapters import CodexRenderer
from pathlib import Path

class LanguageTests(unittest.TestCase):
    def test_renderer_supports_language_and_keeps_chinese_default(self):
        self.assertIn('language', inspect.signature(renderer.render_dashboard).parameters)
        self.assertEqual(renderer.render_dashboard().tobytes(), renderer.render_dashboard(language='zh-CN').tobytes())

    def test_english_frame_preserves_geometry_and_fits_all_text(self):
        self.assertIn('language', inspect.signature(renderer.render_dashboard).parameters)
        calls=[]
        original=renderer.draw_text
        def observed(image, position, text, size, mode, **kwargs):
            dimensions=original(image,position,text,size,mode,**kwargs)
            calls.append((position,text,dimensions))
            return dimensions
        badges=[]
        original_badge=renderer._draw_badge
        def observed_badge(image,box,text,mode,*args,**kwargs):
            badges.append(text)
            mask=renderer.render_text_mask(text,14,mode,font_path=renderer.SOURCE_HAN_BOLD,font_index=0)
            self.assertLessEqual(mask.width,68,text)
            return original_badge(image,box,text,mode,*args,**kwargs)
        with patch.object(renderer,'draw_text',side_effect=observed), patch.object(renderer,'_draw_badge',side_effect=observed_badge):
            en=renderer.render_dashboard(language='en')
        zh=renderer.render_dashboard()
        self.assertEqual(en.size,(400,300))
        self.assertEqual(set(en.getdata()), {renderer.BLACK,renderer.WHITE,renderer.THIRD})
        self.assertEqual(en.crop((0,108,400,124)).tobytes(),zh.crop((0,108,400,124)).tobytes())
        for (x,y),text,(w,h) in calls:
            self.assertLessEqual(x+w,390,text)
        texts=[c[1] for c in calls]+badges
        for text in ['Weekly remaining','30-day usage','Running','Waiting','Queued','Codex Ink']:
            self.assertIn(text,texts)

    def test_language_reaches_real_subprocess_renderer(self):
        self.assertIn('language', inspect.signature(CodexRenderer).parameters)
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'codex'
            shutil.copyfile(Path('Tests/ReliableSyncTests/fixtures/fake_codex.py'),fixture)
            fixture.chmod(0o700)
            en=CodexRenderer(fixture,language='en')({'version':1,'threads':{}})
            zh=CodexRenderer(fixture)({'version':1,'threads':{}})
        self.assertNotEqual(en.crop((0,32,400,148)).tobytes(),zh.crop((0,32,400,148)).tobytes())

    def test_unknown_language_rejected(self):
        self.assertIn('language', inspect.signature(renderer.render_dashboard).parameters)
        with self.assertRaises(ValueError): renderer.render_dashboard(language='fr')
