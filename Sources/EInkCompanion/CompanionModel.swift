import AppKit
import SwiftUI
import Combine
import CryptoKit
import ServiceManagement
import EInkCore

enum LoginItemState: Equatable {
    case enabled, requiresApproval, notRegistered, unavailable
    var label: String {
        switch self {
        case .enabled: return "已启用"
        case .requiresApproval: return "等待系统允许"
        case .notRegistered: return "未启用"
        case .unavailable: return "当前应用无法注册"
        }
    }
    init(_ status: SMAppService.Status) {
        switch status {
        case .enabled: self = .enabled
        case .requiresApproval: self = .requiresApproval
        case .notRegistered: self = .notRegistered
        case .notFound: self = .unavailable
        @unknown default: self = .unavailable
        }
    }
}

@MainActor
final class CompanionModel: ObservableObject {
    static let shared = CompanionModel()
    @Published private(set) var settings: CompanionSettings
    @Published private(set) var configured = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var setup: SetupInspection?
    @Published private(set) var previewImage: NSImage?
    @Published private(set) var lastDataRead: Date?
    @Published private(set) var lastSent: Date?
    @Published private(set) var busy = false
    @Published private(set) var workerReady = false
    @Published private(set) var workerRunning = false
    @Published private(set) var pausing = false
    @Published private(set) var waitingRecovery = false
    @Published private(set) var sleeping = false
    @Published private(set) var stopping = false
    @Published private(set) var loginState = LoginItemState.unavailable
    @Published private(set) var sourceStatus = "尚未读取真实数据"
    @Published var settingsSection: SettingsSection = .default
    let ble = NativeBLEController()
    private(set) var options: CompanionOptions?
    private var store: SettingsStore?
    private var owner: CompanionOwnerLock?
    private let worker = WorkerHost()
    private let helper = HelperRunner()
    private let inspector = HelperRunner()
    private var receiptDrain = WorkerReceiptDrain()
    private var correlation: SessionGuard?
    private var restartPolicy = RestartPolicy()
    private var restartTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var readyDeadline: Task<Void, Never>?
    private var maintenanceEpoch = UUID()
    private var resumeSent = false
    private var allowRecovery = false // Only a user action or observed BLE recovery permits retry:true.
    private var permanentBlocked = false
    private var expectedWorkerExit = false
    private var protocolBlocked = false
    private var initialized = false
    private var inspectionErrorMessage: String?
    private var inspectionUnavailable = false
    @Published private(set) var settingsDamaged = false
    private var cancellables: Set<AnyCancellable> = []

    init(arguments: [String] = Array(CommandLine.arguments.dropFirst())) {
        let codexApp = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") ?? URL(fileURLWithPath: "/Applications/Codex.app")
        let codexPath = codexApp.appendingPathComponent("Contents/Resources/codex").path
        let pythonPath = CompanionSettings.defaultPythonBinary(bundleOverride: Bundle.main.object(forInfoDictionaryKey: "CompanionPythonBinary") as? String)
        settings = CompanionSettings(codexBinary: codexPath, pythonBinary: pythonPath)
        do {
            let options = try CompanionOptions.parse(arguments)
            self.options = options
            store = SettingsStore(directory: options.stateDirectory)
            if let saved = try store?.load() { settings = saved; configured = true }
        } catch { settingsDamaged = true; errorMessage = error.localizedDescription }
        worker.onMessage = { [weak self] message in self?.received(message) }
        worker.onFault = { [weak self] message in
            guard let self else { return }
            self.protocolBlocked = true
            self.errorMessage = message
            self.invalidateSession()
            self.ble.cancel()
        }
        worker.onExit = { [weak self] _ in self?.workerExited() }
        worker.onSessionClosed = { [weak self] in
            self?.invalidateSession()
            self?.ble.cancel()
        }
        ble.onChange = { [weak self] in self?.bluetoothChanged() }
        ble.onRecovery = { [weak self] in
            guard let self, self.gate.mayRun else { return }
            self.allowRecovery = true; self.permanentBlocked = false
            Task { await self.retry() }
        }
        ble.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &cancellables)
    }

    var gate: RunGate {
        RunGate(enabled: settings.syncEnabled && !settingsDamaged, bound: settings.deviceIdentifier != nil,
                setupReady: setup?.isReady == true, inspectionUnavailable: inspectionUnavailable,
                paused: settings.paused, sleeping: sleeping, stopping: stopping || busy)
    }
    var mayEnable: Bool { configured && setup?.isReady == true && settings.deviceIdentifier != nil && !settingsDamaged && !busy && ble.isQuiescent }
    var canMutate: Bool { owner != nil && !settingsDamaged && !busy && !stopping }
    var statusLabel: String {
        if stopping { return "正在退出" }
        if pausing { return "正在暂停" }
        if let _ = errorMessage { return "需要处理" }
        if !configured || settings.deviceIdentifier == nil || setup?.isReady != true { return "待配置" }
        if ble.isSending { return "同步中" }
        if settings.paused || !settings.syncEnabled { return "已暂停" }
        if waitingRecovery || sleeping { return "等待恢复" }
        return workerReady ? "就绪" : "等待恢复"
    }
    enum PrimaryAction {
        case configure(SettingsSection), retry, resume, refresh
        var title: String {
            switch self {
            case .configure: return "完成配置"
            case .retry: return "重试"
            case .resume: return "继续同步"
            case .refresh: return "立即刷新"
            }
        }
    }
    var primaryAction: PrimaryAction {
        if !configured { return .configure(.source) }
        if settings.deviceIdentifier == nil { return .configure(.display) }
        if errorMessage != nil { return .retry }
        if setup?.isReady != true || !settings.syncEnabled { return .configure(.sync) }
        if settings.paused { return .resume }
        if waitingRecovery { return .retry }
        return .refresh
    }
    var primaryLabel: String { primaryAction.title }
    var primaryActionEnabled: Bool {
        if case .configure = primaryAction { return !busy && !stopping }
        return canMutate
    }
    /// Returns a navigation destination for setup; all sending actions retain their own gates.
    func performPrimaryAction() async -> SettingsSection? {
        switch primaryAction {
        case .configure(let section): settingsSection = section; return section
        case .retry: await retry()
        case .resume: await setPaused(false)
        case .refresh: await refresh()
        }
        return nil
    }
    var deviceLabel: String { settings.deviceName.map { "\($0) · 已绑定" } ?? "尚未绑定屏幕" }
    var shouldOpenSettings: Bool { !configured || !settings.syncEnabled || settings.deviceIdentifier == nil || settingsDamaged }

    func start() async {
        guard !initialized else { return }
        initialized = true
        do {
            // Global per-user lock remains shared even with test state-dir overrides.
            let lockURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexEInk/companion-app.lock")
            owner = try CompanionOwnerLock(url: lockURL)
        } catch { errorMessage = error.localizedDescription; return }
        refreshLoginStatus()
        guard !settingsDamaged else { startupDiagnostic("settings_invalid"); return }
        await inspectSetup()
        readJournal()
        await ensureWorker()
        startupDiagnostic("started enabled=\(settings.syncEnabled) paused=\(settings.paused) bound=\(settings.deviceIdentifier != nil) worker=\(worker.isRunning)")
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                guard !Task.isCancelled, let self, !self.stopping else { return }
                self.refreshLoginStatus()
                self.readJournal()
                if !self.busy && !self.sleeping {
                    await self.inspectSetup()
                    // Retry startup when inspection becomes available, preserving crash backoff.
                    if self.restartTask == nil { await self.ensureWorker() }
                    self.reconcileWorker()
                }
            }
        }
    }

    func text(_ source: String) -> String { settings.language.text(source) }

    func setLanguage(_ language: DisplayLanguage) async {
        guard canMutate, language != settings.language else { return }
        busy = true
        guard await quiesce() else { busy = false; return }
        do {
            var updated = settings
            updated.language = language
            try persist(updated)
            previewImage = nil
        } catch {
            errorMessage = error.localizedDescription
            busy = false
            await ensureWorker()
            return
        }
        busy = false
        await preview()
    }

    func savePaths(codex: String, python: String) async {
        guard canMutate else { return }
        busy = true; defer { busy = false }
        guard await quiesce() else { return }
        do {
            guard FileManager.default.isExecutableFile(atPath: codex), FileManager.default.isExecutableFile(atPath: python) else {
                throw CompanionError.unavailable("请选择可执行的 Codex 和 Python 文件；数据/依赖检查由预览完成。")
            }
            var updated = settings
            updated.codexBinary = codex; updated.pythonBinary = python
            // A new data runtime needs explicit re-enabling after a validated preview.
            updated.syncEnabled = false
            try persist(updated)
            errorMessage = nil; protocolBlocked = false; setup = nil; lastDataRead = nil
        } catch { errorMessage = error.localizedDescription }
    }

    func preview() async {
        guard canMutate, let options else { return }
        busy = true
        // Preview-only renderer also uses the journal's single-consumer lock.
        guard await quiesce() else { busy = false; return }
        do {
            let result = try await helper.run(executable: settings.pythonBinary, arguments: options.previewArguments(settings: settings))
            guard result.status == 0,
                  let payload = try JSONSerialization.jsonObject(with: result.stdout) as? [String: Any],
                  payload["status"] as? String == "preview", let stamp = validEpoch(payload["data_read_at"]) else {
                throw CompanionError.unavailable("真实数据检查未通过。请确认 Codex 登录、每周额度和用量历史可读取；旧预览保留。")
            }
            try loadPreview()
            lastDataRead = Date(timeIntervalSince1970: stamp)
            sourceStatus = "账号、每周额度和用量历史检查通过"
            if !configured { try persist(settings) }
            errorMessage = nil
        } catch { errorMessage = error.localizedDescription; sourceStatus = "数据尚未就绪；未发送" }
        busy = false
        await inspectSetup()
        await ensureWorker()
    }

    private func startupDiagnostic(_ value: String) {
        guard ProcessInfo.processInfo.environment["CODEX_INK_UPDATE_DIAGNOSTICS"] == "1" else { return }
        FileHandle.standardError.write(Data(("CodexInk " + value + "\n").utf8))
    }

    func inspectSetup() async {
        guard owner != nil, !settingsDamaged, !inspector.isRunning, !stopping, let options else { return }
        let epoch = maintenanceEpoch
        do {
            let result = try await inspector.run(executable: settings.pythonBinary, arguments: options.setupArguments(action: "inspect", settings: settings), timeout: 10, limit: 65_536, readOnly: true)
            let inspection = try SetupInspection.decode(result.stdout)
            startupDiagnostic("inspection stage=\(inspection.stage) blockers=\(inspection.blockers.joined(separator: ","))")
            guard epoch == maintenanceEpoch, !stopping else { return }
            setup = inspection
            inspectionUnavailable = inspection.isTemporarilyUnavailable
            if result.status != 0 || !inspection.ok {
                setup = nil
                errorMessage = "配置检查失败：" + inspection.blockers.map(SetupBlocker.explanation).joined(separator: "\n")
                inspectionErrorMessage = errorMessage
            } else {
                if errorMessage == inspectionErrorMessage { errorMessage = nil }
                inspectionErrorMessage = nil
            }
        } catch {
            guard epoch == maintenanceEpoch else { return }
            setup = nil
            startupDiagnostic("inspection_transport_failed: \(error.localizedDescription)")
            errorMessage = "无法确认安装状态。" + error.localizedDescription
            if case CompanionError.unavailable = error { inspectionUnavailable = true }
            else { inspectionUnavailable = false }
            inspectionErrorMessage = errorMessage
        }
        if setup?.isReady != true { pauseWorker(); if ble.isSending { ble.cancel() } }
    }

    /// Called only by the explicit confirmed setup/restore buttons, never startup.
    func setupAction(_ action: String) async {
        guard canMutate, ["install", "restore"].contains(action), let options else { return }
        busy = true; defer { busy = false }
        guard await quiesce() else { return }
        do {
            var updated = settings; updated.syncEnabled = false
            try persist(updated)
            setup = nil
            let result = try await helper.run(executable: settings.pythonBinary, arguments: options.setupArguments(action: action, settings: settings), timeout: 30, limit: 65_536)
            let inspection = try SetupInspection.decode(result.stdout)
            setup = inspection
            if result.status != 0 || !inspection.ok {
                errorMessage = "操作未完成：" + inspection.blockers.map(SetupBlocker.explanation).joined(separator: "\n")
            } else {
                errorMessage = nil
                sourceStatus = action == "restore" ? "已请求恢复旧配置；请检查 Codex 信任与旧启动状态。伴侣同步保持关闭。" : "请重新打开 Codex，审阅并信任新 hooks；收到新事件后再检查配置。"
            }
        } catch { errorMessage = error.localizedDescription }
        // Even an install return value never auto-enables; inspector + user action required.
    }

    func scan() async {
        guard canMutate else { return }
        busy = true
        guard await quiesce() else { busy = false; return }
        do { var updated = settings; updated.syncEnabled = false; try persist(updated) }
        catch { errorMessage = error.localizedDescription; busy = false; return }
        busy = false
        ble.scan()
    }

    func bind(_ candidate: BLECandidate) {
        guard canMutate, !worker.isRunning else { return }
        busy = true
        ble.validate(identifier: candidate.id) { [weak self] result in
            guard let self else { return }
            defer { self.busy = false }
            switch result {
            case .success(let verified):
                do {
                    var updated = self.settings
                    try updated.bind(identifier: verified.id, name: verified.name, gattValidated: true, quiescent: self.ble.isQuiescent && !self.worker.isRunning)
                    try self.persist(updated)
                    self.errorMessage = nil
                } catch { self.errorMessage = error.localizedDescription }
            case .failure(let error): self.errorMessage = error.localizedDescription
            }
        }
    }

    func enableSync() async {
        guard canMutate else { return }
        await inspectSetup()
        guard mayEnable else { errorMessage = "请先验证并绑定设备，完成 setup 检查；未信任的新 hooks 不能解锁同步。"; return }
        // The send worker validates live data before every render; preview verifies configuration now.
        guard lastDataRead != nil else { errorMessage = "请先点击“检查数据并生成预览”。"; return }
        do {
            var updated = settings; updated.syncEnabled = true; updated.paused = false
            try persist(updated)
            allowRecovery = true; permanentBlocked = false; protocolBlocked = false; errorMessage = nil
            await ensureWorker()
            reconcileWorker()
        } catch { errorMessage = error.localizedDescription }
    }

    func setPaused(_ paused: Bool) async {
        guard canMutate, settings.syncEnabled else { return }
        do {
            var updated = settings; updated.paused = paused
            try persist(updated)
            if paused {
                pausing = ble.isSending
                pauseWorker() // Current BLE frame can finish; later requests are rejected.
            } else {
                allowRecovery = false; errorMessage = nil
                await inspectSetup()
                await ensureWorker()
                reconcileWorker()
            }
        } catch { errorMessage = error.localizedDescription }
    }

    func refresh() async {
        guard canMutate else { return }
        await inspectSetup()
        guard gate.mayRun else { errorMessage = "尚未启用同步或配置未就绪；立即刷新不会越过配置门禁。"; return }
        if !workerReady { await ensureWorker(); return }
        reconcileWorker()
        guard resumeSent else { return }
        do { try worker.control("refresh") } catch { workerFault(error) }
    }

    func retry() async {
        guard owner != nil, !busy, !stopping, !settingsDamaged else { return }
        errorMessage = nil; protocolBlocked = false; permanentBlocked = false; allowRecovery = true
        restartTask?.cancel(); restartTask = nil; restartPolicy.reset()
        await inspectSetup()
        guard ble.isQuiescent else { errorMessage = "旧蓝牙连接尚未清理；不能开始新的会话。"; return }
        await ensureWorker()
        reconcileWorker()
        if gate.mayRun && workerReady && resumeSent { try? worker.control("refresh") }
    }

    func sleep() {
        sleeping = true; waitingRecovery = true
        allowRecovery = false
        maintenanceEpoch = UUID()
        restartTask?.cancel(); restartTask = nil
        expectedWorkerExit = true
        invalidateSession(); worker.stop(); ble.cancel()
        // Read-only preview may finish safely; no automatic restore/installation is started here.
    }
    func wake() async {
        sleeping = false
        guard settings.syncEnabled && !settings.paused else { return }
        // A fast wake may precede the old child's exit/disconnect callbacks.
        // Those callbacks must now schedule recovery, not remain an expected stop.
        expectedWorkerExit = false
        waitingRecovery = true
        await inspectSetup()
        // Wake is not a blanket reset for a permanent queue failure.
        await ensureWorker()
        reconcileWorker()
    }
    func shutdown() async -> Bool {
        stopping = true
        pollTask?.cancel(); pollTask = nil
        restartTask?.cancel(); restartTask = nil
        pauseWorker()
        // The stopping gate rejects new sends and pending preflight work. Keep
        // this session and maintenance epoch valid until the in-flight receipt
        // is delivered; quiesce invalidates them and cancels BLE afterward.
        let drainDeadline = ProcessInfo.processInfo.systemUptime + 130
        while ble.isSending && ProcessInfo.processInfo.systemUptime < drainDeadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        // A completed radio callback only writes the receipt to stdin. Wait
        // for this worker tick to publish its post-commit status before stop.
        let settlementDeadline = ProcessInfo.processInfo.systemUptime + 10
        while receiptDrain.isPending && worker.isRunning && ProcessInfo.processInfo.systemUptime < settlementDeadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        let drained = !ble.isSending && !receiptDrain.isPending
        let clean = await quiesce()
        if helper.isRunning || inspector.isRunning {
            // Let guarded setup finish its transaction instead of killing it mid-write.
            for _ in 0..<1200 {
                if !helper.isRunning && !inspector.isRunning { break }
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        return drained && clean && !worker.isRunning && !helper.isRunning && !inspector.isRunning
    }

    func refreshLoginStatus() { loginState = LoginItemState(SMAppService.mainApp.status) }
    func setLoginEnabled(_ enabled: Bool) async {
        guard canMutate else { return }
        do {
            if enabled {
                guard configured, let options else { throw CompanionError.unavailable("请先保存配置，再开启登录启动。") }
                try StartupStore.standard.save(stateDirectory: options.stateDirectory)
                try SMAppService.mainApp.register()
            }
            else { try await SMAppService.mainApp.unregister() }
        } catch { errorMessage = "登录启动未更改：" + error.localizedDescription }
        refreshLoginStatus()
    }
    func openLoginSettings() { SMAppService.openSystemSettingsLoginItems() }

    private func persist(_ updated: CompanionSettings) throws {
        guard !settingsDamaged, owner != nil, let store else { throw CompanionError.settingsDamaged }
        try store.save(updated)
        settings = updated; configured = true
    }
    private func ensureWorker() async {
        guard WorkerStartDecision.allowed(ownerExists: owner != nil, gate: gate, workerRunning: worker.isRunning,
                                          bluetoothQuiescent: ble.isQuiescent, protocolBlocked: protocolBlocked),
              let options else { return }
        do {
            let id = UUID().uuidString
            expectedWorkerExit = false
            receiptDrain = WorkerReceiptDrain()
            correlation = SessionGuard(sessionID: id)
            workerReady = false; resumeSent = false
            try worker.start(executable: settings.pythonBinary, arguments: options.bridgeArguments(settings: settings, sessionID: id), sessionID: id)
            workerRunning = true
            readyDeadline?.cancel()
            readyDeadline = Task { [weak self] in
                try? await Task.sleep(for: .seconds(10))
                guard !Task.isCancelled, let self, self.correlation?.sessionID == id, !self.workerReady else { return }
                self.errorMessage = "数据进程未在期限内就绪。"
                self.worker.stop()
            }
        } catch { errorMessage = error.localizedDescription; workerRunning = false; waitingRecovery = true; scheduleRestart() }
    }
    private func received(_ message: WorkerMessage) {
        guard message.sessionID == correlation?.sessionID else { return }
        switch message {
        case .ready(let sessionID):
            do { try correlation?.ready(sessionID: sessionID) }
            catch { workerFault(error); return }
            workerReady = true; waitingRecovery = false
            readyDeadline?.cancel(); readyDeadline = nil
            readJournal()
            reconcileWorker()
        case .status(_, let payload):
            guard workerReady else { workerFault(CompanionError.invalidProtocol); return }
            if let status = payload["status"]?.string { receiptDrain.status(status) }
            if let stamp = payload["data_read_at"]?.number, stamp.isFinite, stamp > 0, stamp <= Date().timeIntervalSince1970 + 60 {
                lastDataRead = Date(timeIntervalSince1970: stamp)
                sourceStatus = "最近一次真实数据读取成功"
                try? loadPreview()
            }
            if let code = payload["error_code"]?.string {
                errorMessage = failureDescription(code)
                if payload["status"]?.string == "blocked" { permanentBlocked = true; allowRecovery = false }
            } else if ["sent", "unchanged", "idle", "preview"].contains(payload["status"]?.string ?? "") {
                if !protocolBlocked { errorMessage = nil }
            }
            readJournal()
        case .send(let sessionID, let requestID, let png, let sha):
            let startedAt = ProcessInfo.processInfo.systemUptime
            do { try correlation?.begin(requestID: requestID, sessionID: sessionID) }
            catch { workerFault(error); return }
            receiptDrain.begin()
            guard gate.mayRun, resumeSent, let identifier = settings.deviceIdentifier else {
                complete(sessionID: sessionID, requestID: requestID, receipt: SendReceipt(errorCode: gate.rejectedRequestCode)); return
            }
            guard ble.isQuiescent else { workerFault(CompanionError.invalidProtocol); return }
            do {
                let frame = try StrictPNG.decode(png, sha256: sha)
                let identity = SendIdentity(sessionID: sessionID, requestID: requestID, bindingRevision: settings.bindingRevision,
                                            deviceIdentifier: identifier, maintenanceEpoch: maintenanceEpoch)
                let installationID = setup?.installationID
                Task { [weak self] in
                    await self?.validateAndSend(frame: frame, identity: identity, installationID: installationID, startedAt: startedAt)
                }
            } catch {
                errorMessage = "帧校验失败：不是校验和匹配的原生 400×300 三色 PNG。"
                complete(sessionID: sessionID, requestID: requestID, receipt: SendReceipt(errorCode: "invalid_frame"))
            }
        }
    }
    private func currentSendIdentity() -> SendIdentity? {
        guard let session = correlation, let request = session.requestID, let identifier = settings.deviceIdentifier,
              workerReady, worker.isRunning, worker.sessionID == session.sessionID else { return nil }
        return SendIdentity(sessionID: session.sessionID, requestID: request, bindingRevision: settings.bindingRevision,
                            deviceIdentifier: identifier, maintenanceEpoch: maintenanceEpoch)
    }
    private func validateAndSend(frame: EInkFrame, identity: SendIdentity, installationID: String?, startedAt: TimeInterval) async {
        guard let options else { return }
        let outcome = await SendPreflight.authorize(identity: identity, installationID: installationID, startedAt: startedAt,
            currentIdentity: { self.currentSendIdentity() },
            mayRun: { self.owner != nil && self.gate.mayRun && self.resumeSent && self.ble.isQuiescent },
            helperBusy: { self.inspector.isRunning || self.helper.isRunning },
            inspect: {
                // Do not call inspectSetup(): its busy early-return has no fresh
                // result and is suitable only for background/UI refreshes.
                let result = try await self.inspector.run(executable: self.settings.pythonBinary,
                    arguments: options.setupArguments(action: "inspect", settings: self.settings), timeout: 10, limit: 65_536, readOnly: true)
                let checked = try SetupInspection.decode(result.stdout)
                guard result.status == 0 || !checked.isReady else { throw CompanionError.invalidProtocol }
                return checked
            }, send: { checked, timeout in
                self.setup = checked
                self.ble.send(frame: frame, identifier: identity.deviceIdentifier, timeout: timeout) { [weak self] receipt in
                    guard let self, self.currentSendIdentity() == identity else { return }
                    self.complete(sessionID: identity.sessionID, requestID: identity.requestID, receipt: receipt)
                }
            })
        guard currentSendIdentity() == identity else { return }
        switch outcome {
        case .allowed, .superseded: break
        case .rejected(let code, let detail, let checked):
            if code == "configuration_error" || code == "device_not_configured" {
                setup = checked?.isReady == false ? checked : nil
                pauseWorker()
            }
            complete(sessionID: identity.sessionID, requestID: identity.requestID, receipt: SendReceipt(errorCode: code))
            errorMessage = detail
        }
    }
    private func complete(sessionID: String, requestID: String, receipt: SendReceipt) {
        guard correlation?.complete(requestID: requestID, sessionID: sessionID) == true else { return }
        do {
            try worker.sendResult(sessionID: sessionID, requestID: requestID, receipt: receipt)
            receiptDrain.deliveredReceipt()
        } catch { workerFault(error) }
        if !receipt.ok { errorMessage = failureDescription(receipt.errorCode ?? "send_failed") }
        pausing = false
        // lastSent is read only from the journal after the worker accepts the receipt.
    }
    private func reconcileWorker() {
        guard workerReady, !protocolBlocked else { return }
        if !gate.mayRun { pauseWorker(); return }
        guard let command = ResumeDecision.command(ready: workerReady, resumed: resumeSent, allowed: gate.mayRun, explicitRecovery: allowRecovery) else { return }
        do {
            try worker.control(command, retry: command == "resume" && allowRecovery)
            resumeSent = true; allowRecovery = false; waitingRecovery = false
        } catch { workerFault(error) }
    }
    private func pauseWorker() {
        if workerReady && resumeSent { do { try worker.control("pause") } catch { workerFault(error) } }
        resumeSent = false
    }
    private func workerFault(_ error: any Error) {
        errorMessage = error.localizedDescription
        protocolBlocked = true
        invalidateSession(); ble.cancel(); worker.stop()
    }
    private func invalidateSession() {
        readyDeadline?.cancel(); readyDeadline = nil
        correlation = nil; workerReady = false; resumeSent = false
    }
    private func workerExited() {
        invalidateSession(); workerRunning = false
        ble.cancel()
        guard !expectedWorkerExit && !stopping && !sleeping else { return }
        waitingRecovery = true
        errorMessage = errorMessage ?? "数据进程已退出；等待蓝牙清理后重试。"
        scheduleRestart()
    }
    private func bluetoothChanged() {
        if ble.isQuiescent { pausing = false }
        if waitingRecovery && !expectedWorkerExit { scheduleRestart() }
    }
    private func scheduleRestart() {
        guard restartTask == nil,
              let delay = restartPolicy.delay(processExited: !worker.isRunning, bleQuiescent: ble.isQuiescent,
                                               allowed: !stopping && !sleeping && !busy && !protocolBlocked && configured && !settingsDamaged) else { return }
        restartTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled, let self else { return }
            self.restartTask = nil
            self.allowRecovery = false
            self.readJournal()
            await self.inspectSetup()
            await self.ensureWorker()
        }
    }
    private func quiesce() async -> Bool {
        maintenanceEpoch = UUID()
        restartTask?.cancel(); restartTask = nil
        expectedWorkerExit = true
        invalidateSession(); worker.stop(); ble.cancel()
        for _ in 0..<60 {
            if !worker.isRunning && ble.isQuiescent { return true }
            try? await Task.sleep(for: .milliseconds(100))
        }
        errorMessage = "旧进程或蓝牙连接尚未清理；已阻止后续操作。"
        return false
    }
    private func loadPreview() throws {
        guard let options else { return }
        let path = options.stateDirectory.appendingPathComponent("preview.png")
        let data = try Data(contentsOf: path)
        let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        _ = try StrictPNG.decode(data, sha256: hash)
        guard let image = NSImage(data: data) else { throw CompanionError.unavailable("无法加载真实预览。") }
        previewImage = image
    }
    private func readJournal() {
        guard let options else { return }
        let path = options.stateDirectory.appendingPathComponent("sync-journal.json")
        guard FileManager.default.fileExists(atPath: path.path) else { return }
        do {
            let data = try Data(contentsOf: path)
            guard data.count <= 65_536, let journal = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  journal["version"] as? Int == 1 else { throw CompanionError.invalidProtocol }
            if let stamp = validEpoch(journal["last_sent_at"]) { lastSent = Date(timeIntervalSince1970: stamp) }
            if let count = journal["failure_count"] as? Int, count > 0, journal["retry_at"] is NSNull { permanentBlocked = true }
            else { permanentBlocked = false }
        } catch { permanentBlocked = true; errorMessage = "同步账本不可读取；未确认下发状态。" }
    }
    private func validEpoch(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite,
              number.doubleValue > 0, number.doubleValue <= Date().timeIntervalSince1970 + 60 else { return nil }
        return number.doubleValue
    }
    private func failureDescription(_ code: String) -> String {
        switch code {
        case "permission_denied": return "蓝牙权限未允许。请在系统设置中允许伴侣应用后重试。"
        case "bluetooth_off": return "蓝牙已关闭。开启蓝牙后重试。"
        case "source_unavailable", "source_state_invalid": return "真实数据暂不可用；保留设备旧画面。请检查 Codex 登录和数据读取。"
        case "device_not_configured": return "未满足设备绑定或安全接管条件；没有发送。"
        case "invalid_frame": return "帧校验未通过；没有发送。"
        case "send_timeout": return "发送或断开超时；未确认画面更新。"
        default: return "同步未完成（\(code)）；保留未确认请求，可重试。"
        }
    }
}
