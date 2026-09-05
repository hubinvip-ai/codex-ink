# Acknowledgements / 致谢

Codex Ink was made possible by the work shared by the following projects, authors and communities. Thank you for making hardware research, protocol exploration, image rendering and local tooling accessible.

感谢以下项目、作者和社区公开分享硬件、协议、字体与工具。你们的工作帮助 Codex Ink 从设备识别、方案验证走到原生蓝牙同步。

## Special thanks / 特别致谢

Special thanks to [CodexBar](https://github.com/steipete/CodexBar), **Peter Steinberger (steipete) and contributors**, for their open-source work making Codex and Claude Code usage visible on macOS.

特别感谢 [CodexBar](https://github.com/steipete/CodexBar) 的作者 **Peter Steinberger（steipete）及贡献者**，感谢他们在 macOS 上呈现 Codex、Claude Code 使用情况方面的开源工作。

## Current implementation and tooling / 当前实现与工具链

| Project / 项目 | Authors or maintainers / 作者或维护者 | Contribution to Codex Ink / 帮助 |
|---|---|---|
| [4.2寸蓝牙电子相册&日历](https://oshwhub.com/sakading/4-2-cun-lan-ya-dian-zi-xiang-ce) | sakading and contributors | The key reference for the existing DA14585 display setup and `webtool.rar` transfer sequence. 为当前 DA14585 设备方案与双位平面传输提供关键参考。 |
| [Source Han Sans / 思源黑体](https://github.com/adobe-fonts/source-han-sans) | Adobe and the Source Han Sans contributors | Chinese glyphs for the native dashboard. The Bold font is included in the app with its license. 提供原生看板中文字体，应用随附 Bold 字体与许可。 |
| [Pillow](https://github.com/python-pillow/Pillow) | Jeffrey A. Clark and Pillow contributors; PIL by Fredrik Lundh, Secret Labs AB and contributors | Image composition, text masks and PNG rendering. 用于图像合成、文字掩模及 PNG 渲染。 |
| [FreeType](https://freetype.org/) | The FreeType Project contributors | Font rasterization and hinting through Pillow. 通过 Pillow 支撑原始像素尺寸下的字形栅格化与 hinting。 |
| [OpenAI Codex](https://github.com/openai/codex) | OpenAI and Codex contributors | The CLI/app-server and hook interfaces used to read local task and usage metadata, as well as a development tool. 提供本项目使用的本地接口与开发工具；此处致谢开源 Codex 项目，不将整个桌面产品称为本项目依赖包。 |
| [Python / CPython](https://github.com/python/cpython) | Python Software Foundation and Python contributors | The worker, renderer orchestration and test runtime. 支撑数据 worker、渲染调度及测试。 |
| [Swift](https://github.com/swiftlang/swift) | The Swift project contributors | The language and toolchain for the native app and BLE implementation. 支撑原生应用及蓝牙实现的语言和编译工具链。 |

## Hardware and protocol research / 硬件与协议研究

These projects helped us compare possible routes and understand display hardware and BLE behavior during early development. They are credited as research references, not presented as installed firmware or packaged runtime dependencies.

以下项目在早期设备识别、路线比较和协议研究中提供了帮助。这里按参考贡献致谢，不把研究过的路线写成当前已安装的固件或打包依赖。

| Project / 项目 | Authors or maintainers / 作者或维护者 | What we learned / 参考内容 |
|---|---|---|
| [三色墨水屏桌面摆件 / 随身挂件](https://oshwhub.com/enzo-and-aqin/mo-shui-ping-m-2) | enzo-and-aqin project page | The initial hardware/control reference supplied for this project; helped distinguish a serial/Air001 route from the final BLE route. 最初提供的硬件控制方案，帮助确认串口路线与最终 BLE 路线的区别。 |
| [EPDTools](https://gitee.com/temperspace/epdtools_-cli) | temperspace and contributors | C++/OpenCV tri-color image conversion and serial transfer research. 研究红黑分离取模、三色图像处理与串口传输。 |
| [OpenEPaperLink](https://github.com/OpenEPaperLink/OpenEPaperLink), [Tag_FW_nRF52811](https://github.com/OpenEPaperLink/Tag_FW_nRF52811) and project wiki | OpenEPaperLink contributors | Display/tag identification, nRF52811 firmware and alternative access-point architecture. 用于价签识别及替代固件/AP 路线评估；最终方案未刷入这套固件。 |
| [ATC_TLSR_Paper](https://github.com/atc1441/ATC_TLSR_Paper) | Aaron Christophel (atc1441) and contributors | BLE shelf-label firmware, transfer and image-layout research. 用于蓝牙价签、传图与位图组织方式研究。 |
| [walmart-esl-flipper](https://github.com/dbzx6r/walmart-esl-flipper) | dbzx6r and contributors | Early BLE protocol and device-family comparisons, including the protocol documentation and Python client. 用于早期 BLE 协议和设备系列比对。最终设备发送序列以 sakading 工程的附件及实机验证为依据。 |

## Scope and licenses / 范围与许可证

This list is grounded in the current source, installed rendering stack, development records and explicitly requested acknowledgements. Search-result recommendations or libraries merely named in another project's README are not automatically classified as dependencies of Codex Ink.

致谢依据为当前源码、实际渲染环境、开发记录及维护者明确提出的致谢。仅出现在搜索结果或上游 README 中的其他库，不自动算作本项目使用的依赖。

Acknowledgement does not replace a license notice, establish license compatibility or imply endorsement. See [third-party notices](THIRD_PARTY_NOTICES.md) and the [attribution evidence record](docs/attribution-audit.md) for versions, scope and unresolved items.

致谢不代替许可证，也不表示这些作者为 Codex Ink 背书。来源、版本与待核对事项见[第三方说明](THIRD_PARTY_NOTICES.md)及[致谢依据记录](docs/attribution-audit.md)。
