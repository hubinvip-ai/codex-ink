import AppKit
import SwiftUI

@main
struct CompanionApp: App {
    @NSApplicationDelegateAdaptor(CompanionAppDelegate.self) private var delegate
    @StateObject private var model = CompanionModel.shared
    var body: some Scene {
        MenuBarExtra {
            CompanionMenu(model: model, showSettings: { delegate.showSettings() })
        } label: {
            HStack(spacing: 4) {
                Image(nsImage: menuBarIcon).accessibilityHidden(true)
                Text("Codex Ink · \(model.statusLabel)")
            }
        }
        .menuBarExtraStyle(.menu)
    }
    private var menuBarIcon: NSImage {
        if let url = Bundle.main.url(forResource: "CodexInkMenuBarTemplate", withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            image.isTemplate = true
            return image
        }
        return NSImage(systemSymbolName: "rectangle.inset.filled", accessibilityDescription: "Codex Ink") ?? NSImage(size: NSSize(width: 18, height: 18))
    }
}

@MainActor
final class CompanionAppDelegate: NSObject, NSApplicationDelegate {
    private var settingsWindow: NSWindow?
    private var observers: [NSObjectProtocol] = []
    private var quitting = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let model = CompanionModel.shared
        if model.shouldOpenSettings { showSettings() }
        let center = NSWorkspace.shared.notificationCenter
        observers.append(center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in CompanionModel.shared.sleep() }
        })
        observers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in await CompanionModel.shared.wake() }
        })
        Task { await model.start() }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showSettings(); return true }
    func showSettings() {
        if settingsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 700), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "Codex Ink · 设置"
            window.contentView = NSHostingView(rootView: CompanionSettingsView(model: .shared))
            window.minSize = NSSize(width: 820, height: 620)
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        Task {
            let clean = await CompanionModel.shared.shutdown()
            if !clean {
                let alert = NSAlert()
                alert.messageText = "仍未确认蓝牙连接清理"
                alert.informativeText = "退出会结束本应用的蓝牙所有权。未完成发送不会确认为成功；下次启动需重试。"
                alert.addButton(withTitle: "退出")
                alert.runModal()
            }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

struct CompanionMenu: View {
    @ObservedObject var model: CompanionModel
    var showSettings: () -> Void
    var body: some View {
        Text(model.statusLabel)
        Text(model.deviceLabel)
        Text("数据读取：\(formatted(model.lastDataRead))")
        Text("成功下发：\(formatted(model.lastSent))")
        if let error = model.errorMessage { Text(error) }
        Divider()
        Button(model.primaryLabel) {
            Task { if await model.performPrimaryAction() != nil { showSettings() } }
        }.disabled(!model.primaryActionEnabled)
        Button(model.settings.paused ? "继续同步" : "暂停同步") { Task { await model.setPaused(!model.settings.paused) } }
            .disabled(!model.settings.syncEnabled || model.busy || model.stopping)
        Button("查看预览") { showSettings(); Task { await model.preview() } }.disabled(model.busy || model.stopping)
        Button("设置…", action: showSettings).keyboardShortcut(",")
        Divider()
        Button("退出 Codex Ink") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }
    private func formatted(_ date: Date?) -> String { date?.formatted(date: .abbreviated, time: .standard) ?? "尚无成功记录" }
}

struct CompanionSettingsView: View {
    @ObservedObject var model: CompanionModel
    @State private var codexPath = ""
    @State private var pythonPath = ""
    @State private var confirmedAction: String?
    @FocusState private var focusedPath: PathField?
    enum PathField { case codex, python }

    var body: some View {
        NavigationSplitView {
            List(SettingsSection.allCases, selection: $model.settingsSection) { section in
                Label(section.title, systemImage: section.symbol)
                    .tag(section)
                    .accessibilityIdentifier("companion.section.\(section.rawValue)")
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 240)
            .safeAreaInset(edge: .bottom, spacing: 0) { sidebarFooter }
        } detail: {
            detailPage
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .windowBackgroundColor))
        }
        .navigationSplitViewStyle(.balanced)
        .frame(minWidth: 820, minHeight: 620)
        .onAppear {
            codexPath = model.settings.codexBinary
            pythonPath = model.settings.pythonBinary
            model.refreshLoginStatus()
        }
        .confirmationDialog(
            confirmedAction == "restore" ? "停止伴侣同步并恢复本应用旧配置？" : "备份并接管本应用的 hooks 与旧启动项？",
            isPresented: Binding(get: { confirmedAction != nil }, set: { if !$0 { confirmedAction = nil } }),
            titleVisibility: .visible
        ) {
            let action = confirmedAction ?? "install"
            Button(action == "restore" ? "停止并恢复" : "确认接管", role: action == "restore" ? .destructive : nil) {
                confirmedAction = nil
                Task { await model.setupAction(action) }
            }
            Button("取消", role: .cancel) { confirmedAction = nil }
        } message: {
            Text("不会自动退出 Codex、绕过 hooks 信任或覆盖其他应用的 hooks。条件不满足时停止并保留原配置。")
        }
    }

    @ViewBuilder private var detailPage: some View {
        switch model.settingsSection {
        case .overview: overviewPage
        case .source: sourcePage
        case .display: displayPage
        case .sync: syncPage
        }
    }

    private var sidebarFooter: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: 10) {
                Image(nsImage: NSApp.applicationIconImage)
                    .resizable().interpolation(.high).frame(width: 26, height: 26)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Codex Ink").font(.callout.weight(.medium))
                    Text(appVersion).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Circle().fill(statusColor).frame(width: 8, height: 8)
                    .accessibilityLabel("当前状态：\(model.statusLabel)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .background(.bar)
    }

    private var overviewPage: some View {
        page(title: "概览", subtitle: "查看屏幕内容与同步状态") {
            HStack(alignment: .top, spacing: 22) {
                previewCard
                VStack(spacing: 14) {
                    statusCard
                    primaryActions
                }
                .frame(maxWidth: .infinity)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var previewCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                if let image = model.previewImage {
                    Image(nsImage: image)
                        .interpolation(.none)
                        .resizable()
                        .aspectRatio(4 / 3, contentMode: .fit)
                        .accessibilityLabel("真实数据生成的 400×300 墨水屏预览，非已下发确认")
                } else {
                    ZStack {
                        RoundedRectangle(cornerRadius: 8).fill(.quaternary)
                        VStack(spacing: 8) {
                            Image(systemName: "rectangle.dashed").font(.title)
                            Text("尚无有效预览").font(.headline)
                            Text("先到“数据源”检查真实数据").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .aspectRatio(4 / 3, contentMode: .fit)
                }
                Text("预览不代表设备已更新；下发成功也不替代实屏确认。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } label: {
            Label("墨水屏预览", systemImage: "rectangle.inset.filled")
        }
        .frame(width: 420)
    }

    private var statusCard: some View {
        GroupBox {
            VStack(spacing: 0) {
                statusRow("运行状态", model.statusLabel, color: statusColor)
                Divider().padding(.vertical, 10)
                statusRow("绑定设备", model.deviceLabel)
                Divider().padding(.vertical, 10)
                statusRow("最近读取", dateLabel(model.lastDataRead))
                Divider().padding(.vertical, 10)
                statusRow("最近下发", dateLabel(model.lastSent))
            }
            .frame(maxWidth: .infinity)
        } label: {
            Label("当前状态", systemImage: "waveform.path.ecg")
        }
    }

    private var primaryActions: some View {
        VStack(spacing: 10) {
            Button(model.primaryLabel) { performPrimaryAction() }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .frame(maxWidth: .infinity)
                .disabled(!model.primaryActionEnabled)
            if model.settings.syncEnabled {
                Button(model.settings.paused ? "继续同步" : "暂停同步") {
                    Task { await model.setPaused(!model.settings.paused) }
                }
                .controlSize(.large)
                .frame(maxWidth: .infinity)
                .disabled(!model.canMutate)
            }
        }
    }

    private var sourcePage: some View {
        page(title: "数据源", subtitle: "连接 Codex 并验证真实数据") {
            GroupBox {
                VStack(alignment: .leading, spacing: 14) {
                    pathRow("Codex 可执行文件", value: $codexPath, field: .codex)
                    pathRow("Python 可执行文件", value: $pythonPath, field: .python)
                    HStack {
                        Button("保存路径") {
                            Task { await model.savePaths(codex: codexPath, python: pythonPath) }
                        }
                        .disabled(!model.canMutate)
                        Button("检查数据并生成预览") { Task { await model.preview() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(!model.canMutate)
                            .accessibilityIdentifier("companion.preview")
                    }
                    Divider()
                    Label(model.sourceStatus, systemImage: model.lastDataRead == nil ? "circle.dashed" : "checkmark.circle.fill")
                        .foregroundStyle(model.lastDataRead == nil ? Color.secondary : Color.green)
                    Text("只读取 Codex 现有登录状态，不保存密码，不调用模型。首次配置只生成预览，不发送画面。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } label: {
                Label("本地运行环境", systemImage: "terminal")
            }
        }
    }

    private var displayPage: some View {
        page(title: "墨水屏", subtitle: "扫描并绑定唯一的发送目标") {
            GroupBox {
                VStack(alignment: .leading, spacing: 14) {
                    LabeledContent("当前设备", value: model.deviceLabel)
                    if let identifier = model.settings.deviceIdentifier {
                        LabeledContent("本机标识") {
                            Text(identifier.uuidString).font(.caption.monospaced()).textSelection(.enabled)
                        }
                    }
                    Divider()
                    HStack {
                        Button("扫描设备") { Task { await model.scan() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(!model.canMutate || !model.ble.isQuiescent)
                            .accessibilityIdentifier("companion.scan")
                        if !model.ble.isQuiescent {
                            Button("停止蓝牙操作") { model.ble.cancel() }
                        }
                    }
                    Text(model.ble.detail).foregroundStyle(.secondary)
                    if model.ble.canSelect {
                        Divider()
                        ForEach(model.ble.candidates) { candidate in
                            HStack {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(candidate.name).font(.headline)
                                    Text(candidate.id.uuidString).font(.caption.monospaced()).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("验证并绑定") { model.bind(candidate) }
                                    .disabled(!model.canMutate)
                                    .accessibilityLabel("验证并绑定 \(candidate.name)，\(candidate.id.uuidString)")
                            }
                        }
                    }
                    Text("名称只供辨认。连接后会验证服务、写入特征及包长，确认断开后才保存本机 UUID。扫描或重绑会关闭自动同步。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } label: {
                Label("设备绑定", systemImage: "display")
            }
        }
    }

    private var syncPage: some View {
        page(title: "自动同步", subtitle: "管理 Hooks、发送状态和登录启动") {
            GroupBox {
                VStack(alignment: .leading, spacing: 12) {
                    LabeledContent("安装检查", value: setupLabel)
                    Text(model.setup?.legacySummary ?? "旧方案检查尚未完成")
                        .foregroundStyle(.secondary)
                    if let setup = model.setup {
                        ForEach(setup.blockers, id: \.self) {
                            Label(SetupBlocker.explanation($0), systemImage: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange).textSelection(.enabled)
                        }
                    }
                    HStack {
                        Button("检查接管状态") { Task { await model.inspectSetup() } }
                            .disabled(!model.canMutate)
                        Button("安装 / 接管…") { confirmedAction = "install" }
                            .buttonStyle(.borderedProminent)
                            .disabled(!model.canMutate)
                    }
                    Text("接管会备份并替换本应用已识别的 Hooks，保留其他 Hooks。请自行退出旧 Codex 会话；应用不会替你关闭 Codex。")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("接管后重新打开 Codex，审阅并信任新 Hooks。只有真实检查为 ready 才能启用同步。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } label: {
                Label("Hooks 与接管", systemImage: "point.3.connected.trianglepath.dotted")
            }

            GroupBox {
                VStack(alignment: .leading, spacing: 12) {
                    if !model.settings.syncEnabled {
                        Button("启用同步") { Task { await model.enableSync() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(!model.mayEnable || model.lastDataRead == nil)
                            .accessibilityIdentifier("companion.enable")
                    } else {
                        HStack {
                            Button(model.settings.paused ? "继续同步" : "暂停同步") {
                                Task { await model.setPaused(!model.settings.paused) }
                            }
                            .disabled(!model.canMutate)
                            Button("立即刷新") { Task { await model.refresh() } }
                                .buttonStyle(.borderedProminent)
                                .disabled(!model.gate.mayRun)
                        }
                    }
                    Toggle("登录时打开", isOn: Binding(
                        get: { model.loginState == .enabled || model.loginState == .requiresApproval },
                        set: { enabled in Task { await model.setLoginEnabled(enabled) } }
                    ))
                    .disabled(!model.canMutate)
                    .accessibilityIdentifier("companion.login")
                    LabeledContent("系统登录项", value: model.loginState.label)
                    Button("打开系统登录项设置") { model.openLoginSettings() }
                    Text("系统中关闭后不会自动重新注册。登录恢复会沿用当前有效配置目录。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } label: {
                Label("同步与启动", systemImage: "arrow.triangle.2.circlepath")
            }

            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    Button("恢复旧配置…", role: .destructive) { confirmedAction = "restore" }
                        .disabled(!model.canMutate)
                    Text("仅在需要退出当前接管方案时使用。恢复仍会检查冲突并保留其他 Hooks。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } label: {
                Label("维护与恢复", systemImage: "wrench.and.screwdriver")
            }
        }
    }

    private func page<Content: View>(title: String, subtitle: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.title2.weight(.semibold))
                    Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Text(model.statusLabel)
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(statusColor.opacity(0.14), in: Capsule())
                    .foregroundStyle(statusColor)
                    .accessibilityIdentifier("companion.status")
                if model.busy { ProgressView().controlSize(.small).accessibilityLabel("正在执行本地操作") }
            }
            .padding(.horizontal, 24).padding(.vertical, 17)
            Divider()
            if let error = model.errorMessage { errorBanner(error) }
            ScrollView {
                VStack(alignment: .leading, spacing: 18) { content() }
                    .padding(24)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
            }
        }
    }

    private func errorBanner(_ error: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
            Text(error).font(.callout).textSelection(.enabled)
                .accessibilityIdentifier("companion.error")
            Spacer()
            Button("重新检查") { Task { await model.retry() } }.disabled(!model.canMutate)
            if let options = model.options {
                Button("查看状态目录") { NSWorkspace.shared.open(options.stateDirectory) }
            }
        }
        .padding(.horizontal, 24).padding(.vertical, 10)
        .background(Color.red.opacity(0.08))
    }

    private func pathRow(_ title: String, value: Binding<String>, field: PathField) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.subheadline.weight(.medium))
            HStack {
                TextField(title, text: value)
                    .focused($focusedPath, equals: field)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel(title)
                    .disabled(!model.canMutate)
                Button("选择…") {
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = false
                    panel.canChooseFiles = true
                    panel.allowsMultipleSelection = false
                    panel.treatsFilePackagesAsDirectories = true
                    panel.title = "选择\(title)"
                    if panel.runModal() == .OK, let url = panel.url {
                        value.wrappedValue = url.path
                        focusedPath = field
                    }
                }
                .disabled(!model.canMutate)
                .accessibilityLabel("选择\(title)")
            }
        }
    }

    private func statusRow(_ title: String, _ value: String, color: Color = .primary) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title).foregroundStyle(.secondary)
            Spacer()
            Text(value).foregroundStyle(color).multilineTextAlignment(.trailing)
        }
    }

    private func performPrimaryAction() {
        Task { _ = await model.performPrimaryAction() }
    }

    private var setupLabel: String {
        switch model.setup?.stage {
        case "ready": return "已验证，可启用"
        case "needs_install": return "尚未安装"
        case "legacy_active": return "旧方案仍在运行"
        case "awaiting_trust": return "等待 Codex 信任与新事件"
        case "blocked": return "需要处理"
        default: return "尚未确认"
        }
    }

    private var statusColor: Color {
        switch model.statusLabel {
        case "就绪": return .green
        case "同步中": return .blue
        case "需要处理": return .red
        case "等待恢复", "已暂停": return .orange
        default: return .secondary
        }
    }

    private var appVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        return "版本 \(version ?? "开发版")"
    }

    private func dateLabel(_ date: Date?) -> String {
        date?.formatted(date: .abbreviated, time: .shortened) ?? "尚无记录"
    }
}
