# Codex Ink icon assets

状态：2026-09-04 经用户确认并接入开发版应用。

## Brand system

- 彩色 AppIcon：窄白色墨水屏外框、六格额度条（末两格点阵）、一条黑色任务线及一个红色状态块。
- 菜单栏：同一构图的单色 template；macOS 根据菜单栏背景提供前景色，具体状态由相邻文字表示。
- 功能图标：设置界面继续使用同线宽 SF Symbols；颜色只表达状态。

## Files

- `app-icon-master-source.png`：内置 ImageGen 输出的 RGBA 原始图。
- `app-icon-master-1024.png`：清理外部零散透明像素后的 1024×1024 构建主图。
- `AppIcon.iconset/`：macOS 标准 16、32、128、256、512 px 及各 2× PNG，共 10 个文件。
- `CodexInk.icns`：由 `iconutil` 从 iconset 生成，随应用包分发。
- `menu-bar/CodexInkMenuBarTemplate.png` 与 `@2x`：18/36 px 菜单栏模板。
- `menu-bar/CodexInkMenuBarTemplate.svg`：菜单栏图标可编辑矢量源。
- `icon-review-sheet.png`：大小尺寸及明暗菜单栏审核图，不随应用包分发。

## Rebuild

```bash
python3 tools/build_icon_assets.py
iconutil -c icns assets/icons/AppIcon.iconset -o assets/icons/CodexInk.icns
```

生成器只创建图标资产，不修改 Info.plist 或应用包。正式打包由 `tools/build_companion.py` 复制 `CodexInk.icns` 及菜单栏 1×/2× 模板。

## Image generation record

使用内置 ImageGen，从用户选择的方案 1 迭代：将白色外框缩窄约 35%，把内容简化为六格额度、单条任务线和红色状态块，最后移除外部背景并生成真实 alpha。菜单栏模板由确定性绘图脚本生成。
