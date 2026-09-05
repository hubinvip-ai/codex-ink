# Codex Ink 构建与安装

[English](installation.md) · [项目首页](../README.zh-CN.md)

当前为源码构建的开发预览版，仍依赖外部 Python/Pillow。尚无已公证的 DMG，也未完成第二台 Mac 安装验收。

## 1. 准备环境

需要 Swift 6 工具链。最低部署目标为 macOS 14，已验证环境为 Apple Silicon、macOS 26.6.2、Swift 6.3.3。在项目根目录检查：

```bash
git clone https://github.com/hubinvip-ai/codex-ink.git
cd codex-ink
swift --version
xcode-select -p
python3 --version
```

所选 Python 必须能导入 Pillow。已验证 Python 3.9.6、Pillow 11.3.0。可使用本机兼容的 Python 创建独立环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'from PIL import Image; print(Image.__version__)'
```

在应用中选择该解释器，并保持路径稳定；移动项目目录也会移动其中的 `.venv`。构建器不会自动安装这些依赖。

保持 Codex 已登录。所选程序须支持项目使用的账号、额度、任务、用量历史及 hooks 目录接口。接口可用性取决于安装版本，预览失败不能解释为用量为零。

## 2. 构建与放置应用

```bash
python3 tools/build_companion.py
```

输出给出 `Codex Ink.app` 路径。包内包含 Swift 程序、Python 脚本、字体及字体许可，采用 ad-hoc 开发签名。构建不会启动应用、安装 hooks 或开启登录启动。

配置 hooks 前，使用 Finder 将应用移到稳定位置，例如 `~/Applications/Codex Ink.app`。不要从临时构建目录安装 hooks；已有同名应用时先阅读更新说明。

开发签名不等于 Developer ID 签名或 Apple 公证。其他 Mac 可能需要额外审阅或授权，分发体验仍未验收；不要为绕过报错而关闭系统安全保护。

## 3. 预览与绑定设备

1. 打开设置，选择实际 Codex/Python 程序，点击“保存路径”。
2. 点击“检查数据并生成预览”，确认真实数据可读取。
3. 点击“扫描设备”，审阅系统蓝牙授权并选择设备。
4. 等待服务、特征验证和断开确认，再确认绑定结果。

设备名称不能证明兼容。当前只支持已记录的 400 × 300 黑白红 BLE 协议；预览成功也不等于屏幕已刷新。

## 4. 安装 hooks 并启用同步

1. 点击“检查接管状态”，查看现有 hooks、旧服务及其他发送者。
2. 在方便中断工作时自行退出 Codex 会话，点击“安装 / 接管…”并审阅确认内容。
3. 重新打开 Codex，通过其自身入口审阅并信任新 hooks。
4. 正常触发一次任务事件。安装回执、hooks 信任及其他检查通过后，才可启用同步。
5. 启用同步后检查真实屏幕效果。

应用保留无关 hooks；来源无法验证或存在其他发送者时停止接管。不要清空 Codex 配置来绕过阻断。

## 5. 登录启动与恢复

保存有效配置后开启“登录时打开”。应用先将当前配置目录记录到 `~/Library/Application Support/CodexEInk/startup.json`，再登记系统登录项。此后普通启动无需额外参数即可恢复同一配置。

关闭登录启动保留目录选择，不会自动重新注册。记录或目标配置损坏、丢失时提示处理，不生成替代配置。本机已验证无参数重启与自动下发；真实注销/重新登录仍待验收。

## 更新已有应用

- 替换应用前正常退出 Codex Ink。
- 保持应用与 Python 路径稳定；hooks 会验证脚本路径和内容，替换应用包可能改变这些文件。
- 若旧 hooks 仍指向 `EInkCompanion.app/Contents/Resources/runtime/`，继续保留原址文件。可见应用改名后，这些文件仍有用途。
- 脚本或路径变化可能需要受控恢复/接管及重新信任；本预览版没有自动升级迁移功能。
- 早期自定义 `--state-dir` 安装升级时，先沿用原参数启动一次，再关闭并开启“登录时打开”，保存目录选择。

恢复和接管细节见[操作说明](companion-operator.md)。避免旧 CLI、手机控制器与伴侣应用同时发送到同一屏幕。

## 常见问题

| 现象 | 处理方向 |
|---|---|
| 缺少 PIL | 用设置中实际选择的 Python 检查 Pillow |
| 无法生成真实预览 | 检查 Codex 登录、路径和接口能力 |
| 接管被阻止 | 根据具体来源、进程或信任错误处理，遵循受控接管流程 |
| 找不到屏幕 | 检查电源、蓝牙授权、协议兼容性和其他控制器 |
| startup.json 报错 | 修复启动记录及目标设置，不会自动生成替代配置 |

反馈请提供版本、失败步骤和脱敏错误；分享前阅读[数据与隐私说明](PRIVACY.md)。
