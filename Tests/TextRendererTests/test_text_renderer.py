import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from tools.codex_status_core import DashboardSnapshot, DisplayStatus, DisplayTask
from tools.eink_text_renderer import (
    ComparisonStage,
    FONT_SIZES,
    FINAL_PROFILE,
    SOURCE_HAN_MEDIUM,
    REQUIRED_TEXT,
    TextMode,
    comparison_profile,
    dashboard_section_bands,
    dashboard_vertical_anchors,
    draw_text,
    render_comparison_stage,
    render_dashboard,
    render_font_test,
    render_four_way_comparison,
    render_text_mask,
    quota_cell_spans,
    usage_bar_heights,
)


class TextRendererTests(unittest.TestCase):
    def test_font_test_is_native_binary_400_by_300(self):
        image = render_font_test()
        self.assertEqual(image.size, (400, 300))
        self.assertEqual(set(image.getdata()), {(255, 255, 255), (0, 0, 0), (198, 40, 40)})
        self.assertEqual(FONT_SIZES, (10, 11, 12, 13, 14, 16))
        self.assertEqual(
            REQUIRED_TEXT,
            ("当前任务", "黑水屏状态刷新", "云端状态同步", "天气与空气质量", "等待指令", "运行中", "排队中"),
        )

    def test_mono_and_grayscale_threshold_are_distinct_binary_masks(self):
        mono = render_text_mask("天气与空气质量", 12, TextMode.MONO)
        threshold = render_text_mask("天气与空气质量", 12, TextMode.GRAYSCALE_THRESHOLD)
        self.assertEqual(mono.mode, "1")
        self.assertEqual(threshold.mode, "1")
        self.assertNotEqual(mono.tobytes(), threshold.tobytes())

    def test_threshold_and_embolden_profiles_make_small_text_progressively_darker(self):
        threshold_144 = render_text_mask("云端状态同步", 12, TextMode.GRAYSCALE_THRESHOLD, threshold=144)
        threshold_128 = render_text_mask("云端状态同步", 12, TextMode.GRAYSCALE_THRESHOLD, threshold=128)
        emboldened = render_text_mask("云端状态同步", 12, TextMode.GRAYSCALE_THRESHOLD, threshold=128, embolden=0.5)
        ink = lambda image: sum(image.getdata())
        self.assertGreaterEqual(ink(threshold_128), ink(threshold_144))
        self.assertGreater(ink(emboldened), ink(threshold_128))

    def test_fractional_text_coordinates_are_rejected(self):
        image = Image.new("RGB", (400, 300), "white")
        with self.assertRaises(ValueError):
            draw_text(image, (10.5, 12), "当前任务", 12, TextMode.GRAYSCALE_THRESHOLD)

    def test_dashboard_renderer_keeps_final_canvas_and_binary_palette(self):
        image = render_dashboard(TextMode.GRAYSCALE_THRESHOLD)
        self.assertEqual(image.size, (400, 300))
        self.assertEqual(set(image.getdata()), {(255, 255, 255), (0, 0, 0), (198, 40, 40)})

    def test_dashboard_renderer_accepts_real_snapshot_without_mock_rows(self):
        snapshot = DashboardSnapshot(
            remaining_percent=55,
            used_percent=45,
            reset_at_epoch=1788771680,
            usage_buckets=(10, 20, 40, 80),
            lifetime_tokens=13_493_155_058,
            updated_at_epoch=1788274235,
            tasks=(
                DisplayTask("thr_1", "Codex Ink", "实现真实状态同步", DisplayStatus.RUNNING, 1788274235),
            ),
        )
        real = render_dashboard(TextMode.GRAYSCALE_THRESHOLD, data=snapshot)
        demo = render_dashboard(TextMode.GRAYSCALE_THRESHOLD)
        self.assertEqual(real.size, (400, 300))
        self.assertNotEqual(real.tobytes(), demo.tobytes())
        self.assertEqual(set(real.getdata()), {(255, 255, 255), (0, 0, 0), (198, 40, 40)})

    def test_dashboard_vertical_anchors_follow_the_four_pixel_grid(self):
        anchors = dashboard_vertical_anchors()
        flattened = [value for values in anchors.values() for value in values]
        self.assertTrue(all(value % 4 == 0 for value in flattened), anchors)
        self.assertEqual(anchors["header"][:2], (8, 8))
        self.assertEqual(anchors["quota"][1], 60)
        self.assertEqual(anchors["task_rows"], (176, 216, 256))

    def test_reference_layout_uses_the_new_section_bands(self):
        self.assertEqual(
            dashboard_section_bands(),
            {"header": (0, 32), "quota": (32, 124), "usage": (124, 176), "tasks": (176, 300)},
        )

    def test_quota_cells_fill_the_complete_380_pixel_row_without_tail_gap(self):
        spans = quota_cell_spans()
        self.assertEqual(len(spans), 50)
        self.assertEqual(spans[0][0], 10)
        self.assertEqual(spans[-1][1], 390)
        self.assertEqual({right - left for left, right in spans}, {6, 7})
        self.assertTrue(all(spans[index + 1][0] - spans[index][1] == 1 for index in range(49)))

    def test_nonzero_usage_bars_keep_reference_minimum_height(self):
        self.assertEqual(usage_bar_heights((0, 1, 3, 9)), (1, 4, 5, 16))

    def test_update_time_shares_usage_heading_row_without_touching_chart(self):
        frame = render_dashboard()
        expected = Image.new('RGB', (400, 300), 'white')
        draw_text(expected, (304, 128), '最后更新 10:28', 12,
                  TextMode.GRAYSCALE_THRESHOLD, profile=FINAL_PROFILE, weight='regular')
        self.assertEqual(frame.crop((304, 128, 390, 148)).tobytes(),
                         expected.crop((304, 128, 390, 148)).tobytes())

    def test_third_task_subtitle_is_complete_and_bottom_margin_is_clear(self):
        frame = render_dashboard()
        expected = Image.new('RGB', (400, 300), 'white')
        draw_text(expected, (10, 272), '天气与空气质量', 12,
                  TextMode.GRAYSCALE_THRESHOLD, profile=FINAL_PROFILE, weight='regular')
        self.assertEqual(frame.crop((10, 272, 302, 288)).tobytes(),
                         expected.crop((10, 272, 302, 288)).tobytes())
        self.assertTrue(all(pixel == (255, 255, 255) for pixel in frame.crop((0, 290, 400, 300)).getdata()))

    def test_header_is_rendered_at_y8_for_account_plan_and_date(self):
        frame = render_dashboard()
        self.assertTrue(all(pixel == (255, 255, 255) for pixel in frame.crop((0, 0, 400, 8)).getdata()))
        expected = Image.new("RGB", (400, 28), "white")
        size = draw_text(expected, (14, 8), "demo@example.com", 12,
                         TextMode.GRAYSCALE_THRESHOLD, profile=FINAL_PROFILE)
        draw_text(expected, (14 + size[0] + 8, 8), "Pro 20x", 12,
                  TextMode.GRAYSCALE_THRESHOLD, profile=FINAL_PROFILE)
        draw_text(expected, (310, 8), "09月01日 周二", 12,
                  TextMode.GRAYSCALE_THRESHOLD, profile=FINAL_PROFILE)
        self.assertEqual(frame.crop((0, 0, 400, 28)).tobytes(), expected.tobytes())

    def test_semibold_small_text_avoids_extra_threshold_darkening(self):
        production = render_dashboard(TextMode.GRAYSCALE_THRESHOLD)
        overdark = render_dashboard(
            TextMode.GRAYSCALE_THRESHOLD,
            profile=replace(FINAL_PROFILE, small_threshold=128),
        )
        black_pixels = lambda image: sum(pixel == (0, 0, 0) for pixel in image.getdata())
        self.assertLess(black_pixels(production), black_pixels(overdark))

    def test_real_bold_increases_text_ink_without_changing_quota_pattern(self):
        production = render_dashboard(TextMode.GRAYSCALE_THRESHOLD)
        semibold = render_dashboard(
            TextMode.GRAYSCALE_THRESHOLD,
            profile=replace(FINAL_PROFILE, medium_font=SOURCE_HAN_MEDIUM, bold_font=SOURCE_HAN_MEDIUM,
                            regular_font=SOURCE_HAN_MEDIUM, medium_index=0, bold_index=0, regular_index=0),
        )
        black_pixels = lambda image: sum(pixel == (0, 0, 0) for pixel in image.getdata())
        self.assertGreater(black_pixels(production), black_pixels(semibold))
        self.assertEqual(production.crop((10, 108, 390, 116)).tobytes(),
                         semibold.crop((10, 108, 390, 116)).tobytes())

    def test_four_comparison_profiles_change_only_typography_parameters(self):
        current = comparison_profile(ComparisonStage.CURRENT)
        size = comparison_profile(ComparisonStage.SIZE)
        font = comparison_profile(ComparisonStage.FONT_WEIGHT)
        tuned = comparison_profile(ComparisonStage.THRESHOLD_EMBOLDEN)
        self.assertEqual(current.core_min_size, 12)
        self.assertEqual(current.project_size, 14)
        self.assertEqual(current.status_size, 12)
        self.assertEqual(current.metric_label_size, 12)
        self.assertEqual(size.core_min_size, 14)
        self.assertEqual(size.project_size, 14)
        self.assertEqual(size.status_size, 14)
        self.assertEqual(size.metric_label_size, 14)
        self.assertEqual(size.subtitle_size, 12)
        self.assertFalse(size.show_usage_heading)
        self.assertEqual(font.medium_font.name, "SourceHanSansCN-Medium.otf")
        self.assertEqual(font.bold_font.name, "SourceHanSansCN-Bold.otf")
        self.assertEqual(tuned.small_threshold, 128)
        self.assertEqual(tuned.large_threshold, 144)
        self.assertEqual(tuned.small_embolden, 0.25)

    def test_comparison_outputs_keep_layout_canvas_unchanged(self):
        images = [render_comparison_stage(stage) for stage in ComparisonStage]
        self.assertTrue(all(image.size == (400, 300) for image in images))
        self.assertEqual(render_four_way_comparison().size, (1600, 324))
        self.assertNotEqual(images[0].tobytes(), images[-1].tobytes())

    def test_saved_font_test_is_not_resized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font-test.png"
            render_font_test().save(path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (400, 300))


if __name__ == "__main__":
    unittest.main()
