#!/usr/bin/env python3
"""Native 400x300 text renderer for the tri-color e-ink dashboard."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tools.codex_status_core import DashboardSnapshot, DisplayStatus


WIDTH = 400
HEIGHT = 300
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
THIRD = (198, 40, 40)
THRESHOLD = 144
ROOT = Path(__file__).resolve().parents[1]
SOURCE_HAN_MEDIUM = ROOT / "assets/fonts/SourceHanSansCN-Medium.otf"
SOURCE_HAN_BOLD = ROOT / "assets/fonts/SourceHanSansCN-Bold.otf"
FONT_SIZES = (10, 11, 12, 13, 14, 16)
REQUIRED_TEXT = (
    "当前任务",
    "黑水屏状态刷新",
    "云端状态同步",
    "天气与空气质量",
    "等待指令",
    "运行中",
    "排队中",
)

_DASHBOARD_VERTICAL_ANCHORS = {
    "header": (8, 8, 28),
    "quota": (40, 60, 64, 88, 108),
    "usage": (128, 148, 168, 176),
    "task_rows": (176, 216, 256),
}

_DASHBOARD_SECTION_BANDS = {
    "header": (0, 32),
    "quota": (32, 124),
    "usage": (124, 176),
    "tasks": (176, 300),
}


class TextMode(str, Enum):
    MONO = "mono"
    GRAYSCALE_THRESHOLD = "grayscale-threshold"


class ComparisonStage(str, Enum):
    CURRENT = "current"
    SIZE = "size"
    FONT_WEIGHT = "font-weight"
    THRESHOLD_EMBOLDEN = "threshold-embolden"


@dataclass(frozen=True)
class RenderProfile:
    stage: ComparisonStage
    medium_font: Path
    bold_font: Path
    medium_index: int
    bold_index: int
    core_min_size: int
    project_size: int
    status_size: int
    metric_label_size: int
    subtitle_size: int
    show_usage_heading: bool
    small_threshold: int
    large_threshold: int
    small_embolden: float
    regular_font: Path | None = None
    regular_index: int = 0


def comparison_profile(stage: ComparisonStage) -> RenderProfile:
    if stage is ComparisonStage.CURRENT:
        return RenderProfile(stage, SOURCE_HAN_MEDIUM, SOURCE_HAN_MEDIUM, 0, 0, 12, 14, 12, 12, 12, True, 144, 144, 0.0)
    if stage is ComparisonStage.SIZE:
        return RenderProfile(stage, SOURCE_HAN_MEDIUM, SOURCE_HAN_MEDIUM, 0, 0, 14, 14, 14, 14, 12, False, 144, 144, 0.0)
    if stage is ComparisonStage.FONT_WEIGHT:
        return RenderProfile(stage, SOURCE_HAN_MEDIUM, SOURCE_HAN_BOLD, 0, 0, 14, 14, 14, 14, 12, False, 144, 144, 0.0)
    return RenderProfile(stage, SOURCE_HAN_MEDIUM, SOURCE_HAN_BOLD, 0, 0, 14, 14, 14, 14, 12, False, 128, 144, 0.25)


FINAL_PROFILE = RenderProfile(
    ComparisonStage.CURRENT,
    SOURCE_HAN_BOLD,
    SOURCE_HAN_BOLD,
    0,
    0,
    14,
    14,
    14,
    14,
    12,
    True,
    144,
    144,
    0.0,
    SOURCE_HAN_BOLD,
    0,
)


def dashboard_vertical_anchors() -> dict[str, tuple[int, ...]]:
    return {name: tuple(values) for name, values in _DASHBOARD_VERTICAL_ANCHORS.items()}


def dashboard_section_bands() -> dict[str, tuple[int, int]]:
    return dict(_DASHBOARD_SECTION_BANDS)


def quota_cell_spans() -> tuple[tuple[int, int], ...]:
    """Return 50 half-open integer spans covering x=10..<390 with 1px gaps."""
    available_cell_pixels = 380 - 49
    spans = []
    left = 10
    for index in range(50):
        width = ((index + 1) * available_cell_pixels) // 50 - (index * available_cell_pixels) // 50
        right = left + width
        spans.append((left, right))
        left = right + (1 if index < 49 else 0)
    return tuple(spans)


def _font(size: int, path: Path = SOURCE_HAN_MEDIUM, index: int = 0) -> ImageFont.FreeTypeFont:
    if not isinstance(size, int):
        raise ValueError("font size must be an integer")
    return ImageFont.truetype(str(path), size=size, index=index, layout_engine=ImageFont.Layout.BASIC)


def render_text_mask(
    text: str,
    size: int,
    mode: TextMode,
    *,
    threshold: int = THRESHOLD,
    embolden: float = 0.0,
    font_path: Path = SOURCE_HAN_MEDIUM,
    font_index: int = 0,
) -> Image.Image:
    """Rasterize once at final size; return a binary glyph mask without dithering."""
    if not 0 <= embolden <= 1:
        raise ValueError("embolden must be between 0 and 1")
    font = _font(size, font_path, font_index)
    left, top, right, bottom = font.getbbox(text, anchor="lt")
    mask_size = (max(1, right - left), max(1, bottom - top))
    if mode is TextMode.MONO:
        mask = Image.new("1", mask_size, 0)
        ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=1, anchor="lt")
        return mask

    grayscale = Image.new("L", mask_size, 0)
    ImageDraw.Draw(grayscale).text((0, 0), text, font=font, fill=255, anchor="lt")
    if embolden > 0:
        expanded = grayscale.filter(ImageFilter.MaxFilter(3))
        grayscale = Image.blend(grayscale, expanded, embolden)
    return grayscale.point(lambda value: 255 if value >= threshold else 0, mode="1")


def _glyph_parameters(profile: RenderProfile, size: int, weight: str) -> tuple[Path, int, int, float]:
    if weight == "bold":
        path, index = profile.bold_font, profile.bold_index
    elif weight == "regular" and profile.regular_font is not None:
        path, index = profile.regular_font, profile.regular_index
    else:
        path, index = profile.medium_font, profile.medium_index
    if size <= 12:
        return path, index, profile.small_threshold, profile.small_embolden
    return path, index, profile.large_threshold, 0.0


def draw_text(
    image: Image.Image,
    position: Tuple[int, int],
    text: str,
    size: int,
    mode: TextMode,
    color: Tuple[int, int, int] = BLACK,
    *,
    profile: RenderProfile | None = None,
    weight: str = "medium",
) -> Tuple[int, int]:
    x, y = position
    if not isinstance(x, int) or not isinstance(y, int):
        raise ValueError("text coordinates must be integer pixels")
    if profile is None:
        mask = render_text_mask(text, size, mode)
    else:
        path, index, threshold, embolden = _glyph_parameters(profile, size, weight)
        mask = render_text_mask(
            text,
            size,
            mode,
            threshold=threshold,
            embolden=embolden,
            font_path=path,
            font_index=index,
        )
    image.paste(color, (x, y), mask)
    return mask.size


def _draw_centered_text(
    image: Image.Image,
    box: Tuple[int, int, int, int],
    text: str,
    size: int,
    mode: TextMode,
    color: Tuple[int, int, int],
    profile: RenderProfile,
) -> None:
    path, index, threshold, embolden = _glyph_parameters(profile, size, "medium")
    mask = render_text_mask(text, size, mode, threshold=threshold, embolden=embolden, font_path=path, font_index=index)
    left, top, right, bottom = box
    x = int(left + (right - left - mask.width) // 2)
    y = int(top + (bottom - top - mask.height) // 2)
    image.paste(color, (x, y), mask)


def _draw_quota_meter(draw: ImageDraw.ImageDraw, remaining: int = 61, top: int = 108) -> None:
    remaining_segments = round(remaining * 50 / 100)
    for index, (left, right) in enumerate(quota_cell_spans()):
        if index < remaining_segments:
            draw.rectangle((left, top, right - 1, top + 7), fill=BLACK)
            continue
        for row in range(8):
            for column in range(right - left):
                if (row + column + index) % 2 == 0:
                    draw.point((left + column, top + row), fill=BLACK)


def usage_bar_heights(values: tuple[int, ...]) -> tuple[int, ...]:
    maximum = max(values, default=0) or 1
    return tuple(1 if value <= 0 else max(4, round(value / maximum * 16)) for value in values)


def _draw_usage_chart(draw: ImageDraw.ImageDraw, values: tuple[int, ...] | None = None) -> None:
    values = values or (2,2,3,1,2,2,1,2,8,3,2,4,6,7,5,9,6,3,2,4,5,2,1,7,9,6,4,3,4,4,3,2,6,1,2,3,2,4,5,3,1,5,3,2,6,6)
    if len(values) < 46:
        values = (0,) * (46 - len(values)) + tuple(values)
    elif len(values) > 46:
        values = tuple(values[-46:])
    for index, height in enumerate(usage_bar_heights(values)):
        color = THIRD if index == 32 else BLACK
        slot_left = 10 + (index * 380) // len(values)
        slot_right = 10 + ((index + 1) * 380) // len(values)
        bar_width = max(2, min(4, slot_right - slot_left - 3))
        left = slot_left + 1
        draw.rectangle((left, 164 - height, left + bar_width - 1, 163), fill=color)


def _draw_dashed_rule(draw: ImageDraw.ImageDraw, y: int) -> None:
    for x in range(10, 390, 4):
        draw.line((x, y, min(x + 1, 389), y), fill=BLACK, width=1)


def _draw_badge(
    image: Image.Image,
    box: Tuple[int, int, int, int],
    text: str,
    mode: TextMode,
    fill: Tuple[int, int, int],
    outline: Tuple[int, int, int],
    text_color: Tuple[int, int, int],
    profile: RenderProfile,
    width: int = 1,
) -> None:
    ImageDraw.Draw(image).rounded_rectangle(box, radius=2, fill=fill, outline=outline, width=width)
    _draw_centered_text(image, box, text, profile.status_size, mode, text_color, profile)


def _format_epoch(epoch: int, pattern: str, fallback: str) -> str:
    if not epoch:
        return fallback
    return datetime.fromtimestamp(epoch).astimezone().strftime(pattern)


def _fit_text(text: str, size: int, max_width: int, profile: RenderProfile, weight: str = "medium") -> str:
    path, index, threshold, embolden = _glyph_parameters(profile, size, weight)
    if render_text_mask(text, size, TextMode.GRAYSCALE_THRESHOLD, threshold=threshold, embolden=embolden, font_path=path, font_index=index).width <= max_width:
        return text
    value = text
    while value:
        candidate = value + "…"
        if render_text_mask(candidate, size, TextMode.GRAYSCALE_THRESHOLD, threshold=threshold, embolden=embolden, font_path=path, font_index=index).width <= max_width:
            return candidate
        value = value[:-1]
    return "…"


def render_dashboard(
    mode: TextMode = TextMode.GRAYSCALE_THRESHOLD,
    profile: RenderProfile = FINAL_PROFILE,
    data: DashboardSnapshot | None = None,
    language: str = "zh-CN",
) -> Image.Image:
    """Draw the approved layout directly on its final 400x300 pixel canvas."""
    if language not in ("zh-CN", "en"):
        raise ValueError("unsupported display language")
    english = language == "en"
    image = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(image)

    date_label = _format_epoch(data.updated_at_epoch if data else 0, "%m月%d日 周%w", "09月01日 周二")
    if data and data.updated_at_epoch:
        weekday = "一二三四五六日"[datetime.fromtimestamp(data.updated_at_epoch).astimezone().weekday()]
        date_label = datetime.fromtimestamp(data.updated_at_epoch).astimezone().strftime("%m月%d日") + f" 周{weekday}"
    if english:
        stamp = datetime.fromtimestamp(data.updated_at_epoch).astimezone() if data and data.updated_at_epoch else None
        date_label = (stamp.strftime("%m/%d") + " " + ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[stamp.weekday()]) if stamp else "09/01 Tue"
    def right_text(y, text, size, *, weight="medium"):
        path, index, threshold, embolden = _glyph_parameters(profile, size, weight)
        width = render_text_mask(text, size, mode, font_path=path, font_index=index, threshold=threshold, embolden=embolden).width
        draw_text(image, (390 - width, y), text, size, mode, profile=profile, weight=weight)
    remaining = data.remaining_percent if data else 61
    used = data.used_percent if data else 39
    reset_label = _format_epoch(data.reset_at_epoch if data else 0, "%m/%d %H:%M", "09/18 15:02")
    updated_label = _format_epoch(data.updated_at_epoch if data else 0, "%H:%M", "10:28")
    account_label = data.account_label if data else "demo@example.com"
    plan_label = data.plan_label if data else "Pro 20x"

    account_text = _fit_text(account_label, 12, 200, profile)
    header_y = _DASHBOARD_VERTICAL_ANCHORS["header"][0]
    account_size = draw_text(image, (14, header_y), account_text, 12, mode, profile=profile)
    draw_text(image, (14 + account_size[0] + 8, header_y), plan_label, 12, mode, profile=profile)
    if english: right_text(header_y, date_label, 12)
    else: draw_text(image, (310, header_y), date_label, 12, mode, profile=profile)
    draw.line((10, 28, 389, 28), fill=BLACK, width=1)

    draw_text(image, (10, 40), "Weekly remaining" if english else "每周额度 · 剩余", 14, mode, profile=profile)
    draw_text(image, (10, 60), f"{remaining}%", 48, mode, profile=profile, weight="bold")
    if english:
        right_text(64, f"Used {used}%", 18)
        right_text(88, f"Resets {reset_label}", 12, weight="regular")
    else:
        draw_text(image, (292, 64), f"已使用 {used}%", 18, mode, profile=profile)
        draw_text(image, (270, 88), f"下次重置 {reset_label}", 12, mode, profile=profile, weight="regular")
    _draw_quota_meter(draw, remaining=remaining, top=108)

    draw_text(image, (10, 128), "30-day usage" if english else "30 天用量", 14, mode, profile=profile)
    _draw_usage_chart(draw, data.usage_buckets if data else None)
    if english: right_text(128, f"Updated {updated_label}", 12, weight="regular")
    else: draw_text(image, (304, 128), f"最后更新 {updated_label}", 12, mode, profile=profile, weight="regular")
    _draw_dashed_rule(draw, 168)

    if data is None:
        rows = (
            (176, "Codex Ink", "墨水屏界面与蓝牙同步", "等待指令", "waiting"),
            (216, "3D 打印机云端服务", "云端状态同步", "运行中", "running"),
            (256, "天气面板", "天气与空气质量", "排队中", "queued"),
        )
    else:
        status_names = {
            DisplayStatus.RUNNING: ("运行中", "running"),
            DisplayStatus.WAITING: ("等待指令", "waiting"),
            DisplayStatus.QUEUED: ("排队中", "queued"),
            DisplayStatus.FAILED: ("失败", "waiting"),
        }
        rows = tuple(
            (_DASHBOARD_VERTICAL_ANCHORS["task_rows"][index], task.project, task.conversation, *status_names[task.status])
            for index, task in enumerate(data.tasks[:3])
        )
    for y, project, conversation, status, state in rows:
        if english:
            status = {"运行中": "Running", "等待指令": "Waiting", "排队中": "Queued", "失败": "Failed"}[status]
        draw_text(image, (10, y), _fit_text(project, profile.project_size, 292, profile), profile.project_size, mode, profile=profile)
        draw_text(image, (10, y + 16), _fit_text(conversation, profile.subtitle_size, 292, profile, weight="regular"), profile.subtitle_size, mode, profile=profile, weight="regular")
        badge = (318, y + 4, 389, y + 27)
        if state == "waiting":
            _draw_badge(image, badge, status, mode, THIRD, THIRD, WHITE, profile)
        elif state == "running":
            _draw_badge(image, badge, status, mode, BLACK, BLACK, WHITE, profile)
        else:
            _draw_badge(image, badge, status, mode, WHITE, BLACK, BLACK, profile, width=2)
        if y != rows[-1][0]:
            _draw_dashed_rule(draw, y + 36)
    return image


def render_comparison_stage(stage: ComparisonStage) -> Image.Image:
    return render_dashboard(TextMode.GRAYSCALE_THRESHOLD, comparison_profile(stage))


def render_four_way_comparison() -> Image.Image:
    labels = ("1 CURRENT", "2 SIZE", "3 SOURCE HAN", "4 THRESHOLD+EMBOLDEN")
    canvas = Image.new("RGB", (WIDTH * 4, HEIGHT + 24), WHITE)
    draw = ImageDraw.Draw(canvas)
    for index, stage in enumerate(ComparisonStage):
        canvas.paste(render_comparison_stage(stage), (index * WIDTH, 24))
        draw.text((index * WIDTH + 8, 5), labels[index], fill=BLACK)
    return canvas


def render_font_test() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw_text(image, (8, 4), "A 直接 MONO", 12, TextMode.MONO)
    draw_text(image, (204, 4), "B 灰阶 + 单阈值", 12, TextMode.GRAYSCALE_THRESHOLD, THIRD)
    samples = (
        (10, "当前任务"),
        (11, "黑水屏状态刷新"),
        (12, "云端状态同步"),
        (13, "天气与空气质量"),
        (14, "等待指令 / 运行中"),
        (16, "排队中"),
    )
    for row, (size, text) in enumerate(samples):
        y = 28 + row * 42
        draw_text(image, (8, y), str(size), 10, TextMode.MONO)
        draw_text(image, (30, y), text, size, TextMode.MONO)
        draw_text(image, (204, y), str(size), 10, TextMode.GRAYSCALE_THRESHOLD)
        draw_text(image, (226, y), text, size, TextMode.GRAYSCALE_THRESHOLD)
    draw_text(image, (8, 280), "单一字体：Source Han CN Medium · 原生 400×300", 10, TextMode.GRAYSCALE_THRESHOLD)
    return image


def _parse_mode(value: str) -> TextMode:
    try:
        return TextMode(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _parse_stage(value: str) -> ComparisonStage:
    try:
        return ComparisonStage(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    font_test = subparsers.add_parser("font-test")
    font_test.add_argument("--output", type=Path, required=True)
    dashboard = subparsers.add_parser("dashboard")
    dashboard.add_argument("--output", type=Path, required=True)
    dashboard.add_argument("--mode", type=_parse_mode, default=TextMode.GRAYSCALE_THRESHOLD)
    dashboard.add_argument("--stage", type=_parse_stage, default=ComparisonStage.THRESHOLD_EMBOLDEN)
    comparison = subparsers.add_parser("comparison")
    comparison.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "font-test":
        image = render_font_test()
    elif args.command == "comparison":
        image = render_four_way_comparison()
    else:
        image = render_dashboard(args.mode, comparison_profile(args.stage))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output, format="PNG", optimize=False)
    print(f"RENDERED path={args.output} size={image.width}x{image.height}")


if __name__ == "__main__":
    main()
