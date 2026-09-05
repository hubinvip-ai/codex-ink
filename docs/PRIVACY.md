# Data and privacy / 数据与隐私

## English

Codex Ink reads account, quota, usage-history and task metadata through the locally installed Codex app-server. The app asks for executable paths and Bluetooth access, not your account password. Its monitoring workflow does not submit model-generation requests.

Hooks persist lifecycle metadata such as task/session identifiers, working directories, event names and timestamps. They do not store prompt or reply bodies. The companion uses available task titles; if a title is missing it displays an unnamed-task label instead of using prompt preview text.

State, settings, migration records, receipts and rendered frames live under the selected local state directory. The usual base is `~/Library/Application Support/CodexEInk/`; custom directories can also be selected. Hook setup modifies the identified entries in `~/.codex/hooks.json` through a backup and review flow. Login startup stores the selected directory in `startup.json`.

The native app sends the rendered frame to the device selected and validated by the user. Account labels, task titles and usage can be visible on that physical display even when the Mac is locked or disconnected.

The companion/worker connection uses private process pipes, not a project-hosted web service. No project analytics endpoint is implemented in this workflow. Codex itself has separate account/network behavior; these statements do not describe all traffic from Codex or macOS.

Before sharing a screenshot, log, support archive or Git history, inspect it for account labels, project/task names, paths, device identifiers and credentials. Do not attach auth files, hook trust records or full runtime directories to public issues. The README image contains fictional demo data.

## 简体中文

Codex Ink 通过本机 Codex app-server 读取账号、额度、用量历史和任务元数据。应用要求选择程序路径及蓝牙授权，不要求输入账号密码；监控流程不会提交模型生成请求。

Hooks 保存任务/会话标识、工作目录、事件和时间等生命周期元数据，不保存提示词或回复正文。伴侣应用优先使用任务标题；标题缺失时显示未命名任务，不以提示词预览代替。

设置、状态、迁移记录、回执和渲染图保存在所选本地状态目录，通常位于 `~/Library/Application Support/CodexEInk/`。受控安装会备份并修改 `~/.codex/hooks.json` 中已识别的条目。`startup.json` 只记录启动时使用的配置目录。

原生应用向用户选择并验证的屏幕发送画面。即使 Mac 已锁定或断开，屏幕仍可能显示账号、任务和用量，需按实际放置场景考虑可见范围。

应用与 worker 通过私有进程管道连接，该流程未实现项目运营的 Web 服务或分析上报端点。Codex 自身的账号与联网行为另行适用，不能据此宣称 Codex 或 macOS 完全离线。

公开截图、日志、支持材料或 Git 历史前，检查账号、项目/任务名、路径、设备标识和凭据。不要向公开 issue 上传认证文件、hooks 信任记录或整个运行目录。首页展示图使用虚构示例数据。
