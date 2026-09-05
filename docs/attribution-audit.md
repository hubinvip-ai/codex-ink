# Source and attribution record

Reviewed 2026-09-05. This record distinguishes protocol implementation references, bundled resources, external tools and research references. It does not replace upstream license texts.

## Main protocol reference

Author: **sakading and contributors**. Project: [4.2寸蓝牙电子相册&日历](https://oshwhub.com/sakading/4-2-cun-lan-ya-dian-zi-xiang-ce).

The project's historical page identified GPL 3.0. The live project page could not be read during the latest verification. The original [webtool.rar attachment](https://image.lceda.cn/attachments/2023/5/V04hu7P1EVzJFcEALkWvoEeLTtKyCXpxKnPnHbKh.rar) was retrieved again on 2026-09-05 and matched the recorded MD5 `2fa7b3bdbdfc53fa90affff49f81c028`.

The attachment contains `epd42.html`, `js/dithering.js` and `js/utils.js`. No separate license or author notice was found inside these files; the recorded license comes from the project page. The attachment is not redistributed here.

`epd42.html` supplies the initialization, image-mode selection, `03 FF` / `03 00` two-plane packets with big-endian offsets, and `01 01` refresh command. These are reflected in `Sources/EInkCore/EPDProtocol.swift` and the frame bitplane encoder. The native implementation adds payload validation and CoreBluetooth transport; the tested sender uses 240-byte payloads rather than the web tool's 450. The upstream web UI and its JavaScript dithering implementation are not shipped. See [protocol notes](ble-protocol.md).

## Shipped resources and external runtime

- Source Han Sans CN 2.005R: Medium and Bold source assets; Bold is packaged. The original SIL OFL 1.1 notice remains in `assets/fonts/` and is copied into the app.
- Pillow 11.3.0: externally installed, pinned in `requirements.txt`; MIT-CMU notice in its distribution.
- FreeType: used through Pillow. The tested runtime reports 2.13.3. Refer to the exact Pillow distribution for bundled native-library notices.
- Swift package: no third-party Swift package dependencies; uses Apple system frameworks.
- Python, Swift and Codex: external toolchain/runtime/interfaces, not vendored source trees.

## Research references

The following were examined during hardware identification and route comparison. No firmware from these projects is shipped or flashed by Codex Ink. This table records the inspected snapshots rather than asserting their current licenses.

| Reference | Inspected revision | Root notice observed |
|---|---|---|
| EPDTools | `5938cdbe27e2cd3952425dc1b1c1dfcb0127ceee` | GPL v3 |
| OpenEPaperLink | `04ed6408cba113baae7f859257376d449b51ee1b` | CC BY-NC-SA 4.0 |
| Tag_FW_nRF52811 | `5c87f6b9c77b9e9d6681478fcb34887beed3a238` | No root license file found |
| ATC_TLSR_Paper | `3778d296f418c30b05310d86dafa4e3404071cb4` | No root license file found |
| walmart-esl-flipper | `889810aac353fac882fee75509ceea1824226c46` | README states MIT, referenced root LICENSE absent |

The initial enzo-and-aqin hardware reference used an Air001/serial route. Its historical page identified GPL 3.0; this was not revalidated live. Project links and each research contribution are listed in [Acknowledgements](../ACKNOWLEDGEMENTS.md).

## CodexBar

The maintainer used CodexBar before building Codex Ink. It inspired the need for visible quota information; the desk display adds task status at the edge of vision. CodexBar is credited to Peter Steinberger (steipete) and contributors. It is not a runtime dependency, and no CodexBar source is vendored.

## Maintenance

Keep [third-party notices](../THIRD_PARTY_NOTICES.md) aligned with actual shipped files. New copied/adapted source must carry its own provenance and applicable notices; acknowledgement alone does not grant rights to reuse it.
