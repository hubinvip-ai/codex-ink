import AppKit

@MainActor
final class ProbeAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var controller: BLEProbeController?
    private var awaitingTermination = false
    private let status = NSTextField(wrappingLabelWithString: "请选择一个新的 JSON 报告路径，再点击开始扫描。")
    private let stages = NSTextField(wrappingLabelWithString: "所有验证项：not-tested")
    private let output = NSTextField(wrappingLabelWithString: "报告：未指定")
    private let hint = NSTextField(labelWithString: "设备提示：NRF_325608（不会自动连接）")
    private let devices = NSPopUpButton(frame: .zero, pullsDown: false)
    private let chooseOutput = NSButton(title: "选择报告位置…", target: nil, action: nil)
    private let start = NSButton(title: "开始扫描", target: nil, action: nil)
    private let connect = NSButton(title: "连接所选设备并验证", target: nil, action: nil)
    private let cancel = NSButton(title: "停止 / 断开", target: nil, action: nil)

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        let arguments = Array(CommandLine.arguments.dropFirst())
        if !arguments.isEmpty {
            do { configure(try ProbeOptions.parse(arguments)) }
            catch { status.stringValue = "参数无效。用法：--report /绝对路径/新报告.json [--device 标识或名称]。也可点击选择报告位置。" }
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func configure(_ options: ProbeOptions) {
        do {
            let controller = try BLEProbeController(options: options)
            self.controller = controller
            controller.onChange = { [weak self] in self?.refresh() }
            controller.onFinished = { [weak self] in
                if self?.awaitingTermination == true { NSApp.reply(toApplicationShouldTerminate: true) }
            }
            chooseOutput.isEnabled = false
            output.stringValue = "报告（每阶段自动保存）：\(options.reportURL.path)"
            hint.stringValue = "设备提示：\(options.deviceHint)（仅排序提示，必须手动选择）"
            refresh()
        } catch {
            status.stringValue = "无法创建报告：请选择不存在且可写的新 JSON 文件。未启动蓝牙。"
        }
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 840, height: 510), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "EInk BLE Probe · 开发实验"
        window.delegate = self
        window.center()
        let title = NSTextField(labelWithString: "独立应用蓝牙验证")
        title.font = .boldSystemFont(ofSize: 22)
        let detail = NSTextField(wrappingLabelWithString: "仅扫描 → 手动选择 → 连接 → GATT 校验 → 确认断开。不会发送画面或修改固件。点击开始扫描后才请求蓝牙权限；系统授权请由你处理。")
        for label in [detail, status, stages, output] { label.preferredMaxLayoutWidth = 792 }
        stages.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        output.isSelectable = true
        output.font = .systemFont(ofSize: 11)
        status.setAccessibilityIdentifier("probe.status")
        devices.setAccessibilityLabel("发现的蓝牙设备：名称、标识及信号强度")
        devices.addItem(withTitle: "请手动选择发现的设备")
        devices.target = self
        devices.action = #selector(selectionChanged)
        devices.widthAnchor.constraint(equalToConstant: 792).isActive = true
        for (button, action) in [(chooseOutput, #selector(selectOutput)), (start, #selector(startScan)), (connect, #selector(connectSelected)), (cancel, #selector(cancelProbe))] {
            button.target = self
            button.action = action
            button.bezelStyle = .rounded
        }
        start.isEnabled = false
        connect.isEnabled = false
        cancel.isEnabled = false
        let actions = NSStackView(views: [chooseOutput, start, connect, cancel])
        actions.orientation = .horizontal
        actions.spacing = 12
        let stack = NSStackView(views: [title, detail, hint, devices, actions, status, stages, output])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: window.contentView!.bottomAnchor, constant: -24),
        ])
        let menu = NSMenu()
        let appMenuItem = NSMenuItem()
        menu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 EInk BLE Probe", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = menu
    }

    @objc private func selectOutput() {
        let panel = NSSavePanel()
        panel.title = "选择新的实验报告文件（不覆盖已有文件）"
        panel.nameFieldStringValue = "ble-probe-\(UUID().uuidString.prefix(8)).json"
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        configure(ProbeOptions(reportURL: url, deviceHint: "NRF_325608"))
    }
    @objc private func startScan() { controller?.start() }
    @objc private func cancelProbe() { controller?.cancel() }
    @objc private func selectionChanged() { updateConnectButton() }
    @objc private func connectSelected() {
        guard let value = devices.selectedItem?.representedObject as? String, let identifier = UUID(uuidString: value) else { return }
        controller?.select(identifier)
    }

    private func updateConnectButton() {
        guard let controller else { connect.isEnabled = false; return }
        connect.isEnabled = [.scanning, .selection].contains(controller.session.phase) && devices.selectedItem?.representedObject != nil && !controller.exportFailed
    }

    private func refresh() {
        guard let controller else { return }
        let session = controller.session
        let previousSelection = devices.selectedItem?.representedObject as? String
        devices.removeAllItems()
        devices.addItem(withTitle: "请手动选择发现的设备（名称 · CoreBluetooth 标识 · RSSI）")
        let deviceHint = controller.options.deviceHint
        let candidates = session.candidates.values.sorted {
            let left = $0.name == deviceHint || $0.identifier.uuidString.caseInsensitiveCompare(deviceHint) == .orderedSame
            let right = $1.name == deviceHint || $1.identifier.uuidString.caseInsensitiveCompare(deviceHint) == .orderedSame
            return left != right ? left : $0.identifier.uuidString < $1.identifier.uuidString
        }
        for candidate in candidates {
            devices.addItem(withTitle: "\(candidate.name) · \(candidate.identifier.uuidString) · \(candidate.rssi) dBm")
            devices.lastItem?.representedObject = candidate.identifier.uuidString
            if candidate.identifier.uuidString == previousSelection { devices.select(devices.lastItem) }
        }
        devices.isEnabled = [.scanning, .selection].contains(session.phase)
        start.isEnabled = session.phase == .idle && !controller.exportFailed
        cancel.isEnabled = ![.idle, .finished, .disconnecting].contains(session.phase)
        updateConnectButton()
        let messages: [ProbePhase: String] = [
            .idle: "就绪。点击开始扫描才会访问蓝牙。",
            .authorization: "等待用户授权：请自行处理系统蓝牙权限；等待授权不会超时。",
            .ready: "等待蓝牙就绪（最多 30 秒），请确认系统蓝牙已开启。",
            .scanning: "扫描中（最多 30 秒）。请选择设备，再点击连接验证。",
            .selection: "扫描已停止。请选择发现的设备；不会自动连接。",
            .connecting: "正在连接所选设备（连接及 GATT 共用 30 秒）。",
            .services: "已连接，正在匹配 EPD 服务。",
            .characteristics: "服务已匹配，正在匹配 EPD 特征；不会读写特征值。",
            .disconnecting: "已请求断开，等待真实断开回调（最多 5 秒）。",
            .finished: "本次实验已结束。查看分项证据；重试请退出并使用新报告路径。",
        ]
        status.stringValue = controller.exportFailed ? "报告写入失败：已停止后续探测并尝试断开；磁盘报告可能停留在上一阶段。" : messages[session.phase]!
        if let error = session.report.errorCode { status.stringValue += "\n错误类别：\(error)" }
        stages.stringValue = ProbeReport.stageNames.map { "\($0): \(session.report.stages[$0]!.rawValue)" }.joined(separator: "   ")
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let controller else { return .terminateNow }
        if controller.session.hasPendingConnection {
            awaitingTermination = true
            controller.cancel()
            return .terminateLater
        }
        controller.cancel()
        return .terminateNow
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApp.terminate(nil)
        return false
    }
}

let application = NSApplication.shared
let delegate = ProbeAppDelegate()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
