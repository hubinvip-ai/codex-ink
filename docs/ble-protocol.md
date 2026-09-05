# NRF_325608 墨水屏 BLE 协议记录

记录日期：2026-09-01

## 来源

- 开源工程：[4.2寸蓝牙电子相册&日历](https://oshwhub.com/sakading/4-2-cun-lan-ya-dian-zi-xiang-ce)
- 工程许可：GPL 3.0
- 协议参考附件：`webtool.rar`
- 附件 MD5：`2fa7b3bdbdfc53fa90affff49f81c028`

本项目没有复制网页工具界面，只根据公开实现复现必要的设备通信和位平面格式。

其他早期硬件与协议研究项目及作者见[完整致谢](../ACKNOWLEDGEMENTS.md)；各自的参考范围、版本与许可核对状态见[致谢依据](attribution-audit.md)。这些研究项目不替代下文针对实际设备记录的发送序列。

## 实机 GATT

- 广播名称：`NRF_325608`
- EPD Service：`13187B10-EBA9-A3BA-044E-83D3217D9A38`
- EPD Characteristic：`4B646063-6264-F3A7-8941-E65356EA82FE`
- Characteristic properties：read、write、notify
- 实测 `maximumWriteValueLength(.withResponse)`：512 bytes
- 实测 `maximumWriteValueLength(.withoutResponse)`：244 bytes

设备不会在广播包中声明 EPD Service，因此扫描必须先按设备名发现，连接后再校验 Service 与 Characteristic。

## 画面数据

- 逻辑画面：400 × 300 px。
- 每 8 个像素按从高位到低位打包为 1 byte。
- 每个位平面：400 × 300 ÷ 8 = 15,000 bytes。
- 黑白平面：白色为 1；黑色和第三色为 0。
- 第三色平面：第三色为 1；白色和黑色为 0。

## 写入序列

1. `00 00`：初始化。
2. `02 00 00`：选择图片模式。
3. `03 FF <offset:2 bytes, big-endian> <black-white payload>`：黑白平面。
4. `03 00 <offset:2 bytes, big-endian> <third-color payload>`：第三色平面。
5. `01 01`：触发全屏刷新。

开源网页工具使用 450-byte payload。实机通过 macOS CoreBluetooth 发送该长度时，DA14585 在第一块画面数据后断开；改用 240-byte payload 后，单包总长为 244 bytes，129/129 包全部写入成功。设备通知 `00F4` 表示收到 244 bytes，最后一块通知 `007C` 表示收到 124 bytes。

成功发送全屏刷新命令后立即断开，避免设备保持连接增加功耗。
