# 可靠同步核心 · 开发验证入口

本入口是产品化第 1 阶段的可测试核心，不是已安装的菜单栏产品。不会自动安装 hooks、注册登录启动、改动旧服务或获取额外权限。

## 隔离状态目录

在项目根目录运行，保留本终端的两个变量：

```bash
EINK_STAGE=$(mktemp -d /tmp/eink-validation.XXXXXX)
EINK_SOURCE="$EINK_STAGE/source/status.json"
```

所有外层命令必须显式提供 `--state-dir` 与 `--state-file`，没有指向正式安装目录的隐式默认值。

登记请求并查看账本（不读账号、不连蓝牙）：

```bash
python3 tools/codex_eink_reliable.py --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" request
python3 tools/codex_eink_reliable.py --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" status
```

输出 `queued` 只表示请求已保存，不能解释为已经刷屏。

## 只读真实预览

下例使用本机已知 Codex 可执行文件；其他安装位置需传入实际路径。源任务文件未创建时没有任务行，不会补示例任务。

```bash
python3 tools/codex_eink_reliable.py \
  --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" \
  preview --codex-binary /Applications/ChatGPT.app/Contents/Resources/codex
```

产物为 `$EINK_STAGE/preview.png`。预览不登记或确认真实发送、不更新最后发送时间；账户数据读取仍使用现有只读 app-server 客户端。

需要查看本机真实任务时，可以仅在 `preview` 命令中把 `--state-file` 指向现有 `~/Library/Application Support/CodexEInk/status.json`。不要把实验 `hook` 入口指向正式文件。

## 事件入口

`hook` 从 stdin 接收现有 Codex 生命周期 JSON，输出中立 `{}`，不做蓝牙操作。只保存会话 ID、回合 ID、项目目录、状态、事件名及时间；不保存 prompt、tool_input 或回复内容。输入错误或存储失败写脱敏错误码到 stderr，不阻止 Codex 正常运行。

用于隔离验证的示例：

```bash
printf '%s\n' '{"session_id":"test-task","turn_id":"test-turn","cwd":"/test/project","hook_event_name":"Stop"}' | \
  python3 tools/codex_eink_reliable.py --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" hook
```

示例 ID 只用于测试登记；现有渲染器只展示有真实任务元数据匹配的任务。上述命令不安装全局 hooks。

## 显式启动消费者

**只有协调好设备占用后再运行本节。** 本阶段的排他锁仅覆盖相同状态目录中的新 worker，不约束旧服务、手机小程序或其他状态目录。不能同时运行多个程序向同一屏幕发送。

```bash
swift build -c release
python3 tools/codex_eink_reliable.py \
  --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" \
  work --codex-binary /Applications/ChatGPT.app/Contents/Resources/codex \
  --push-binary "$PWD/.build/release/eink-push" --device NRF_325608
```

这是复用旧 Swift CLI 的开发入口，不证明独立 `.app` 已获取蓝牙权限；后台启动仍可能受 macOS 权限上下文限制。新的监督进程用原生 PTY 启动发送器，并与发送器共同继承发送锁；即使消费者被强杀，旧发送器退出之前也不会允许新消费者发送。设备名称来自调用者，永久设备标识绑定由后续菜单栏应用提供。

消费者运行时空闲每 5 秒检查一次、每 600 秒登记额度刷新需求；事件合并窗口 30 秒，普通自动发送距上次成功至少 180 秒。发送中的帧不被抢占，下轮取最新快照。Ctrl+C 或 SIGTERM 请求在当前有界操作结束后退出，不删除未完成请求。

需要强制刷新（绕过去重但不解除永久错误）或修复错误后重试：

```bash
python3 tools/codex_eink_reliable.py --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" request --force
python3 tools/codex_eink_reliable.py --state-dir "$EINK_STAGE" --state-file "$EINK_SOURCE" retry
```

## 状态解释

| 状态 | 含义 |
|---|---|
| `queued` | 已登记，消费者可以尚未运行 |
| `pending` | 等待合并窗口到期 |
| `sent` | 当前帧的全部 CLI 写包完成；不等于实屏视觉确认 |
| `unchanged` | 与最后发送像素相同，本次无需刷屏 |
| `idle` | 当前请求已处理，常驻消费者继续待命 |
| `retrying` | 暂时错误，等待持久化退避时间 |
| `blocked` | 权限、蓝牙关闭、配置或文件错误，需修复后重试 |
| `busy` | 同状态目录已有消费者，当前入口没有执行发送 |
| `preview` | 只生成本地预览，没有发送 |

账本中 `requested_revision > acknowledged_revision` 表示仍有待处理请求。`last_sent_at` 只在真实写包成功后更新；去重、排队、预览都不能更新它。错误只写枚举码，保存的账本不含对话文本或邮箱。

## 恢复与边界

- 暂时发送失败按 5/15/30/60 秒退避，随后每 60 秒再试；新事件不会导致无限快速重连。
- 权限拒绝、蓝牙关闭和无设备配置不会快速重试，修复后用 `retry` 显式恢复。显式重启消费者并取得所有权时，对永久错误开放一次恢复尝试；不会每轮清除退避。正式应用的系统状态唤醒属于第 2 阶段。
- 损坏或未知版本的账本不被自动清空；保留文件供诊断，不能靠“恢复初始”伪装成功。
- worker 崩溃后未确认请求仍在；若设备已收到而本地尚未确认，下次允许重发。异常退出可能留下专属实验目录内的帧文件，不自动清理其他目录。
- 只停止新消费者就能停止实验链路；本阶段没有覆盖旧运行时，不需要恢复全局配置。
- 现有每周窗口、套餐显示映射和缺失用量兜底尚未整改；本阶段不宣称账户语义已达到正式发布标准。

## 回归命令

```bash
python3 -m unittest discover -s Tests/ReliableSyncTests -v
python3 -m unittest discover -s Tests/StatusSyncTests -v
python3 -m unittest discover -s Tests/HtmlLayoutTests -v
python3 -m unittest discover -s Tests/TextRendererTests -v
swift run eink-core-tests
```
