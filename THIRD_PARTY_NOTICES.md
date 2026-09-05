# Third-party sources and license status

See [Acknowledgements / 致谢](ACKNOWLEDGEMENTS.md) for the people and projects we thank, and [the attribution audit](docs/attribution-audit.md) for the evidence behind each relationship.

## Project code

Codex Ink project code is licensed under the GNU General Public License version 3 only (`GPL-3.0-only`); see [LICENSE](LICENSE). Copyright (C) 2026 hubinvip-ai and contributors. Upstream resources retain their own notices and terms. The principal protocol reference is credited below and in the attribution record.

## Resources shipped with the app

### Source Han Sans

The repository includes Adobe's [Source Han Sans](https://github.com/adobe-fonts/source-han-sans), release 2.005R according to the [font inventory](assets/fonts/README.md). Both Medium and Bold are present in the source checkout; the current companion packager includes the Bold font.

The [bundled SIL Open Font License 1.1](assets/fonts/LICENSE-SourceHanSans.txt) is included with the app. Preserve the font copyright and license notices when redistributing the font.

## External rendering dependencies

### Pillow / PIL

[Pillow](https://github.com/python-pillow/Pillow) supplies image processing and PNG output. The inspected runtime is Pillow 11.3.0. Its installed license identifies **MIT-CMU** and credits Jeffrey A. Clark and contributors, as well as the original PIL work by Fredrik Lundh, Secret Labs AB and contributors.

Pillow is externally installed, not bundled by the current app packager. Its distribution also contains notices for native libraries; if future app releases bundle Pillow, preserve and review those distribution notices instead of substituting this summary.

### FreeType

[FreeType](https://freetype.org/) supplies font rasterization through Pillow. The inspected Pillow runtime reports FreeType 2.13.3. The installed Pillow license includes the FreeType notice describing the FreeType License and GPL licensing alternatives. Refer to the exact distributed FreeType files when selecting the applicable terms for any future bundled binary.

FreeType is not a separately vendored library in this Swift package. It is credited because the current renderer uses Pillow's FreeType path.

## Hardware and protocol references

| Project | Evidence and observed license status | Use in this project |
|---|---|---|
| [4.2寸蓝牙电子相册&日历](https://oshwhub.com/sakading/4-2-cun-lan-ya-dian-zi-xiang-ce) | Historical page and protocol record identify GPL 3.0; `webtool.rar` MD5 is recorded in [protocol notes](docs/ble-protocol.md) | Main DA14585 device/transfer reference. Packet and bitplane rules reflected in the Swift encoder; scope documented in the attribution record |
| [三色墨水屏桌面摆件 / 随身挂件](https://oshwhub.com/enzo-and-aqin/mo-shui-ping-m-2) | Historical page record identifies GPL 3.0; live page could not be revalidated in this pass | Initial hardware and serial-control comparison |
| [EPDTools](https://gitee.com/temperspace/epdtools_-cli) | Inspected checkout contains a GPL v3 LICENSE | Tri-color conversion and serial-route research |
| [OpenEPaperLink](https://github.com/OpenEPaperLink/OpenEPaperLink) | Inspected root LICENSE is CC BY-NC-SA 4.0; this does not establish the license of every subcomponent | Tag identification and alternative AP architecture research |
| [Tag_FW_nRF52811](https://github.com/OpenEPaperLink/Tag_FW_nRF52811) | No root license file found in the inspected checkout; component terms not audited | Alternative firmware research; not flashed by this project |
| [ATC_TLSR_Paper](https://github.com/atc1441/ATC_TLSR_Paper) | No root license file found in the inspected checkout; per-file/component terms not audited | BLE and image-format research |
| [walmart-esl-flipper](https://github.com/dbzx6r/walmart-esl-flipper) | README states MIT and links to LICENSE, but that file is absent from the inspected root; do not treat the README badge as a complete license record | Early protocol and device-family comparison |

The OSHWHub project pages could not be revalidated in this pass. Their stated licenses above are historical observations, not current live confirmations. The main protocol attachment was retrieved again and its checksum matched. Research acknowledgement does not establish that source code was copied, nor does it certify that no code was adapted.

## External tools and interfaces

### CodexBar — special acknowledgement

[CodexBar](https://github.com/steipete/CodexBar), maintained by Peter Steinberger (steipete) and contributors, is included as a special acknowledgement at the project maintainer's request. Its upstream [LICENSE](https://github.com/steipete/CodexBar/blob/main/LICENSE) is MIT. No CodexBar code reuse or runtime dependency is asserted by this acknowledgement; the MIT reference does not select a license for Codex Ink.

[OpenAI Codex](https://github.com/openai/codex), [Python](https://github.com/python/cpython) and [Swift](https://github.com/swiftlang/swift) are acknowledged as external interfaces, runtimes and development tools. They are not redistributed as source projects by the current app packager. If later distribution changes that boundary, inspect the exact versions' notices.

The current Swift package declares no third-party package dependencies. Apple system frameworks remain platform APIs; this statement is not a claim that all development tools or all Python native dependencies are enumerated as shipped components.

## 中文说明

致谢页已补齐能够从源码和开发记录确认的项目，并区分实际使用、外部工具与方案研究。字体许可随应用分发；Pillow/FreeType 当前由外部环境提供。不同参考项目的许可状态分别记录，不能统一写成 MIT，也不能用致谢代替来源审查。项目代码采用 GPL-3.0-only，见 LICENSE；第三方资源保留各自的版权和许可。
