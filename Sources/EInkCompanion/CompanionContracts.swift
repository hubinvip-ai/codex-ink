import Foundation
import CoreFoundation
import Darwin

enum SettingsSection: String, CaseIterable, Identifiable, Sendable {
    case overview, source, display, sync
    static let `default`: Self = .overview
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: return "概览"
        case .source: return "数据源"
        case .display: return "墨水屏"
        case .sync: return "自动同步"
        }
    }
    var symbol: String {
        switch self {
        case .overview: return "rectangle.grid.2x2"
        case .source: return "terminal"
        case .display: return "rectangle.inset.filled"
        case .sync: return "arrow.triangle.2.circlepath"
        }
    }
}

enum CompanionError: Error, LocalizedError {
    case invalidSettings, settingsDamaged, startupDamaged, invalidArguments, alreadyRunning, invalidProtocol, frameTooLarge, truncatedLine
    case unavailable(String)
    var errorDescription: String? {
        switch self {
        case .invalidSettings: return "设置无效：请检查可执行路径和设备绑定。"
        case .settingsDamaged: return "settings.json 损坏或版本不支持；已停止同步，原文件未覆盖。请修复文件后重试。"
        case .startupDamaged: return "startup.json 无效或所选配置已丢失/损坏；已停止自动恢复。请修复启动记录及其指向的 settings.json 后重新打开，原文件未覆盖。"
        case .invalidArguments: return "仅支持绝对路径参数 --state-dir 和 --runtime-root。"
        case .alreadyRunning: return "此用户已有伴侣应用持有控制权。"
        case .invalidProtocol: return "数据进程协议不匹配；会话已停止。"
        case .frameTooLarge: return "数据进程消息超过 1 MiB，已停止会话。"
        case .truncatedLine: return "数据进程在完整消息前退出。"
        case .unavailable(let message): return message
        }
    }
}

struct CompanionSettings: Codable, Equatable, Sendable {
    static func defaultPythonBinary(bundleOverride: String? = nil,
                                    isExecutable: (String) -> Bool = FileManager.default.isExecutableFile(atPath:)) -> String {
        if let bundleOverride { return bundleOverride }
        // Fresh development installs prefer the system interpreter validated for
        // this runtime. Existing settings and explicit bundle choices win later.
        return ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"].first(where: isExecutable) ?? "/usr/bin/python3"
    }
    var version = 1
    var codexBinary: String
    var pythonBinary: String
    var deviceIdentifier: UUID?
    var deviceName: String?
    var bindingRevision = 0
    var syncEnabled = false
    var paused = false
    var language: DisplayLanguage = .chinese
    enum CodingKeys: String, CodingKey, CaseIterable {
        case version, codexBinary = "codex_binary", pythonBinary = "python_binary"
        case deviceIdentifier = "device_identifier", deviceName = "device_name"
        case bindingRevision = "binding_revision", syncEnabled = "sync_enabled", paused, language
    }
    init(codexBinary: String, pythonBinary: String) {
        self.codexBinary = codexBinary; self.pythonBinary = pythonBinary
    }
    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(Int.self, forKey: .version)
        codexBinary = try c.decode(String.self, forKey: .codexBinary)
        pythonBinary = try c.decode(String.self, forKey: .pythonBinary)
        deviceIdentifier = try c.decodeIfPresent(UUID.self, forKey: .deviceIdentifier)
        deviceName = try c.decodeIfPresent(String.self, forKey: .deviceName)
        bindingRevision = try c.decode(Int.self, forKey: .bindingRevision)
        syncEnabled = try c.decode(Bool.self, forKey: .syncEnabled)
        paused = try c.decode(Bool.self, forKey: .paused)
        language = c.contains(.language) ? try c.decode(DisplayLanguage.self, forKey: .language) : .chinese
    }
    func validated() throws -> Self {
        guard version == 1, codexBinary.hasPrefix("/"), pythonBinary.hasPrefix("/"),
              !codexBinary.contains("\0"), !pythonBinary.contains("\0"), bindingRevision >= 0,
              (deviceIdentifier == nil) == (deviceName == nil),
              deviceIdentifier == nil || (bindingRevision > 0 && !(deviceName?.isEmpty ?? true)),
              !syncEnabled || deviceIdentifier != nil else { throw CompanionError.invalidSettings }
        return self
    }
    mutating func bind(identifier: UUID, name: String, gattValidated: Bool, quiescent: Bool) throws {
        guard gattValidated, quiescent, !name.isEmpty, bindingRevision < Int.max else { throw CompanionError.invalidSettings }
        deviceIdentifier = identifier
        deviceName = name
        bindingRevision += 1
        syncEnabled = false
    }
    func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(version, forKey: .version)
        try c.encode(codexBinary, forKey: .codexBinary)
        try c.encode(pythonBinary, forKey: .pythonBinary)
        try c.encode(deviceIdentifier?.uuidString, forKey: .deviceIdentifier)
        try c.encode(deviceName, forKey: .deviceName)
        try c.encode(bindingRevision, forKey: .bindingRevision)
        try c.encode(syncEnabled, forKey: .syncEnabled)
        try c.encode(paused, forKey: .paused)
        try c.encode(language, forKey: .language)
    }
}

struct SettingsStore {
    let url: URL
    init(directory: URL) { url = directory.appendingPathComponent("settings.json") }
    func load() throws -> CompanionSettings? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do {
            let bytes = try Data(contentsOf: url)
            let object = try StrictJSON.object(bytes)
            guard bytes.count <= 65_536,
                  Set(object.keys).subtracting(["language"]) == Set(CompanionSettings.CodingKeys.allCases.map(\.rawValue)).subtracting(["language"]) else { throw CompanionError.invalidSettings }
            return try JSONDecoder().decode(CompanionSettings.self, from: bytes).validated()
        } catch { throw CompanionError.settingsDamaged }
    }
    func save(_ settings: CompanionSettings) throws {
        // A damaged/unknown file is never silently replaced by defaults.
        _ = try load()
        _ = try settings.validated()
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(settings).write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}

/// Remembers a user's selected configuration without relocating trusted hooks or data.
struct StartupStore {
    let url: URL
    static var standard: Self {
        Self(url: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexEInk/startup.json"))
    }
    private func validate(_ path: String) throws -> URL {
        guard path.hasPrefix("/"), !path.contains("\0"), !path.split(separator: "/").contains("..") else { throw CompanionError.startupDamaged }
        let directory = URL(fileURLWithPath: path, isDirectory: true)
        guard try SettingsStore(directory: directory).load() != nil else { throw CompanionError.startupDamaged }
        return directory
    }
    func load() throws -> URL? {
        let attributes: [FileAttributeKey: Any]
        do { attributes = try FileManager.default.attributesOfItem(atPath: url.path) }
        catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileReadNoSuchFileError { return nil }
        catch { throw CompanionError.startupDamaged }
        do {
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  let size = attributes[.size] as? NSNumber, size.intValue <= 8192 else { throw CompanionError.startupDamaged }
            let data = try Data(contentsOf: url)
            let object = try StrictJSON.object(data)
            guard Set(object.keys) == ["version", "state_directory"],
                  let version = object["version"] as? NSNumber,
                  CFGetTypeID(version) != CFBooleanGetTypeID(), version == 1,
                  let path = object["state_directory"] as? String else { throw CompanionError.startupDamaged }
            return try validate(path)
        } catch { throw CompanionError.startupDamaged }
    }
    func save(stateDirectory: URL) throws {
        // Never replace an unreadable or unknown startup record.
        _ = try load()
        _ = try validate(stateDirectory.path)
        let data = try JSONSerialization.data(withJSONObject: ["version": 1, "state_directory": stateDirectory.path], options: [.prettyPrinted, .sortedKeys])
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}

/// Never unlink a live lock file. CLOEXEC prevents children extending app ownership.
final class CompanionOwnerLock {
    private let fd: Int32
    init(url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        fd = Darwin.open(url.path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw CompanionError.unavailable("无法建立应用所有权锁。") }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            throw CompanionError.alreadyRunning
        }
    }
    deinit { close(fd) }
}

struct CompanionOptions {
    let stateDirectory: URL
    let runtimeRoot: URL
    var hookRuntimeRoot: URL? = nil
    static let bundleID = "com.ben.codex-eink.companion.dev"
    static func parse(_ args: [String], resources: URL? = Bundle.main.resourceURL) throws -> Self {
        var values: [String: String] = [:]
        var i = 0
        while i < args.count {
            let key = args[i]
            guard ["--state-dir", "--runtime-root"].contains(key), values[key] == nil, i + 1 < args.count,
                  args[i + 1].hasPrefix("/") else { throw CompanionError.invalidArguments }
            values[key] = args[i + 1]
            i += 2
        }
        let standard = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexEInk/companion", isDirectory: true)
        guard let root = values["--runtime-root"].map({ URL(fileURLWithPath: $0, isDirectory: true) }) ?? resources?.appendingPathComponent("runtime", isDirectory: true) else { throw CompanionError.invalidArguments }
        let selected = try values["--state-dir"].map { URL(fileURLWithPath: $0, isDirectory: true) } ?? StartupStore.standard.load() ?? standard
        if values["--runtime-root"] == nil, let resources {
            let syncRoot = resources.appendingPathComponent("sync-runtime", isDirectory: true)
            var isDirectory: ObjCBool = false
            if FileManager.default.fileExists(atPath: syncRoot.path, isDirectory: &isDirectory), isDirectory.boolValue {
                return Self(stateDirectory: selected, runtimeRoot: syncRoot, hookRuntimeRoot: root)
            }
        }
        return Self(stateDirectory: selected, runtimeRoot: root)
    }
    var stateFile: URL { stateDirectory.appendingPathComponent("status.json") }
    func script(_ name: String) -> String { runtimeRoot.appendingPathComponent("tools/" + name).path }
    func bridgeArguments(settings: CompanionSettings, sessionID: String) -> [String] {
        [script("companion_bridge.py"), "--state-dir", stateDirectory.path, "--state-file", stateFile.path,
         "--codex-binary", settings.codexBinary, "--session-id", sessionID, "--language", settings.language.rawValue]
    }
    func previewArguments(settings: CompanionSettings) -> [String] {
        [script("codex_eink_reliable.py"), "--state-dir", stateDirectory.path, "--state-file", stateFile.path,
         "preview", "--codex-binary", settings.codexBinary, "--validated", "--language", settings.language.rawValue]
    }
    func setupArguments(action: String, settings: CompanionSettings) -> [String] {
        let hooks = hookRuntimeRoot ?? runtimeRoot
        var args = [hooks.appendingPathComponent("tools/companion_setup.py").path, "--state-dir", stateDirectory.path, action]
        if action == "install" { args += ["--python", settings.pythonBinary, "--runtime-root", hooks.path] }
        return args
    }
}

struct JSONLFramer {
    static let limit = 1_048_576
    private var pending = Data()
    mutating func append(_ data: Data) throws -> [Data] {
        var lines: [Data] = []
        // Bound unfinished input before retaining it; multiple short lines are allowed.
        for byte in data {
            if byte == 10 {
                guard pending.count + 1 <= Self.limit else { throw CompanionError.frameTooLarge }
                lines.append(pending)
                pending = Data()
            } else {
                guard pending.count < Self.limit - 1 else { throw CompanionError.frameTooLarge }
                pending.append(byte)
            }
        }
        return lines
    }
    func finish() throws { if !pending.isEmpty { throw CompanionError.truncatedLine } }
}

enum JSONValue: Codable, Equatable, Sendable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .array(try c.decode([JSONValue].self)) }
    }
    func encode(to encoder: any Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    var string: String? { if case .string(let s) = self { return s }; return nil }
    var number: Double? { if case .number(let n) = self { return n }; return nil }
}

enum WorkerMessage: Sendable {
    case ready(sessionID: String)
    case status(sessionID: String, payload: [String: JSONValue])
    case send(sessionID: String, requestID: String, png: Data, sha256: String)
    var sessionID: String {
        switch self { case .ready(let s), .status(let s, _), .send(let s, _, _, _): return s }
    }
    static func decode(_ data: Data) throws -> Self {
        let object = try StrictJSON.object(data)
        guard data.count + 1 <= JSONLFramer.limit,
              let version = object["version"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(), !["d", "f"].contains(String(cString: version.objCType)), version == 1,
              let type = object["type"] as? String, let session = object["session_id"] as? String,
              !session.isEmpty, session.count <= 128 else { throw CompanionError.invalidProtocol }
        let common: Set<String> = ["version", "type", "session_id"]
        switch type {
        case "ready":
            guard Set(object.keys) == common else { throw CompanionError.invalidProtocol }
            return .ready(sessionID: session)
        case "status":
            guard Set(object.keys) == common.union(["payload"]), let payload = object["payload"] as? [String: Any] else { throw CompanionError.invalidProtocol }
            let typed = try JSONDecoder().decode([String: JSONValue].self, from: JSONSerialization.data(withJSONObject: payload))
            return .status(sessionID: session, payload: typed)
        case "send":
            guard Set(object.keys) == common.union(["request_id", "png_base64", "png_sha256"]),
                  let id = object["request_id"] as? String, !id.isEmpty, id.count <= 128,
                  let encoded = object["png_base64"] as? String, let png = Data(base64Encoded: encoded), !png.isEmpty,
                  let sha = object["png_sha256"] as? String, sha.count == 64, sha.allSatisfy({ "0123456789abcdef".contains($0) }) else { throw CompanionError.invalidProtocol }
            return .send(sessionID: session, requestID: id, png: png, sha256: sha)
        default: throw CompanionError.invalidProtocol
        }
    }
}

struct SessionGuard {
    let sessionID: String
    private(set) var isReady = false
    private(set) var requestID: String?
    private var seen: Set<String> = []
    init(sessionID: String) { self.sessionID = sessionID }
    mutating func ready(sessionID: String) throws {
        guard sessionID == self.sessionID, !isReady else { throw CompanionError.invalidProtocol }
        isReady = true
    }
    mutating func begin(requestID: String, sessionID: String) throws {
        guard isReady, sessionID == self.sessionID, self.requestID == nil, !seen.contains(requestID), seen.count < 100_000 else { throw CompanionError.invalidProtocol }
        seen.insert(requestID)
        self.requestID = requestID
    }
    mutating func complete(requestID: String, sessionID: String) -> Bool {
        guard sessionID == self.sessionID, self.requestID == requestID else { return false }
        self.requestID = nil
        return true
    }
}

struct SendReceipt: Sendable {
    var packets: Int = 0
    var bytes: Int = 0
    var disconnected = false
    var errorCode: String?
    var ok: Bool { errorCode == nil && packets == 129 && bytes == 30_511 && disconnected }
}

enum BridgeOutput {
    static func control(_ type: String, sessionID: String, retry: Bool = false) throws -> Data {
        guard ["pause", "resume", "stop", "refresh"].contains(type), !retry || type == "resume" else { throw CompanionError.invalidProtocol }
        var object: [String: Any] = ["version": 1, "type": type, "session_id": sessionID]
        if retry { object["retry"] = true }
        return try encode(object)
    }
    static func result(sessionID: String, requestID: String, receipt: SendReceipt) throws -> Data {
        var value: [String: Any] = ["version": 1, "type": "send_result", "session_id": sessionID, "request_id": requestID, "ok": receipt.ok]
        if receipt.ok { value.merge(["packets": receipt.packets, "bytes": receipt.bytes, "disconnected": true]) { _, n in n } }
        else { value["error_code"] = receipt.errorCode ?? "send_failed" }
        return try encode(value)
    }
    private static func encode(_ object: [String: Any]) throws -> Data {
        var data = try JSONSerialization.data(withJSONObject: object, options: .sortedKeys)
        data.append(10)
        return data
    }
}

struct SetupInspection: Decodable, Sendable {
    let ok: Bool
    let stage: String
    let blockers: [String]
    let installationID: String?
    let legacyPresent: Bool
    enum CodingKeys: String, CodingKey { case ok, stage, blockers, installationID = "installation_id", legacyPresent = "legacy_present" }
    var isReady: Bool { ok && stage == "ready" && blockers.isEmpty && installationID != nil }
    var isTemporarilyUnavailable: Bool {
        let transient: Set<String> = ["process_inspection_failed", "source_api_unavailable", "config_changed", "migration_busy", "launchagent_inspection_failed"]
        return !ok && stage == "blocked" && !blockers.isEmpty && blockers.allSatisfy(transient.contains)
    }
    var legacySummary: String {
        guard ok, stage != "blocked", blockers.isEmpty else { return "旧方案检查尚未完成" }
        return legacyPresent ? "检测到旧方案配置。" : "未检测到已识别的旧方案配置。"
    }
    static func decode(_ data: Data) throws -> Self {
        let object = try StrictJSON.object(data)
        guard Set(object.keys) == ["ok", "stage", "blockers", "installation_id", "legacy_present"] else { throw CompanionError.invalidProtocol }
        let result = try JSONDecoder().decode(Self.self, from: data)
        guard ["needs_install", "legacy_active", "awaiting_trust", "ready", "blocked"].contains(result.stage) else { throw CompanionError.invalidProtocol }
        return result
    }
}

enum SetupBlocker {
    static func explanation(_ code: String) -> String {
        let guidance: String
        switch code {
        case "codex_running": guidance = "请自行退出 Codex 会话后再接管；应用不会代为关闭。"
        case "sender_running": guidance = "旧发送者仍在运行，请等待本次发送结束。"
        case "companion_running": guidance = "另一个伴侣进程仍在运行，请先退出它。"
        case "legacy_active": guidance = "旧启动项或旧 hooks 仍在活动，接管尚未完成。"
        case "unsafe_path": guidance = "状态目录必须位于当前用户目录中，不能使用 /tmp、符号链接或不安全路径。"
        case "runtime_location_unstable": guidance = "应用运行目录位于临时目录；正式接管前请移到稳定位置。"
        case "unknown_hook_source": guidance = "存在无法安全识别的 hooks 来源；请先核查，不会自动覆盖。"
        case "config_changed", "owned_hook_conflict", "rollback_conflict": guidance = "配置在检查后发生变化；请核对当前文件与备份，禁止整体覆盖。"
        case "config_invalid", "file_invalid": guidance = "配置文件不可解析；请先修复原文件。"
        case "migration_busy": guidance = "另一个接管操作尚未结束，请稍后重新检查。"
        case "migration_incomplete": guidance = "接管存在未完成步骤；请核对备份和状态后恢复。"
        case "source_changed", "source_invalid", "installation_conflict": guidance = "运行脚本或安装路径与记录不符；请核对稳定运行目录。"
        case "installation_invalid", "receipt_invalid": guidance = "安装记录或新 hook 事件凭据无效；请重新检查配置与信任流程。"
        case "process_inspection_failed": guidance = "无法确认旧进程已停止；为防止双发送，接管被阻止。"
        case "legacy_agent_conflict", "launchagent_inspection_failed", "bootout_failed": guidance = "无法安全确认或停用本应用旧启动项；请检查启动项状态。"
        case "storage_error", "backup_too_large": guidance = "无法安全读取或备份配置；请检查文件权限、大小与可用空间。"
        default: guidance = "条件未满足，请核对本地安装记录后重试。"
        }
        return "\(guidance)（\(code)）"
    }
}

/// JSONSerialization alone accepts duplicate keys and normalizes them. Preflight
/// the bounded grammar so an ambiguous control document never reaches a decoder.
enum StrictJSON {
    static func object(_ data: Data) throws -> [String: Any] {
        guard data.count <= JSONLFramer.limit, String(data: data, encoding: .utf8) != nil else { throw CompanionError.invalidProtocol }
        var scanner = Scanner(bytes: [UInt8](data))
        try scanner.value(depth: 0)
        scanner.space()
        guard scanner.index == data.count, let object = try JSONSerialization.jsonObject(with: data) as? [String: Any], finite(object) else { throw CompanionError.invalidProtocol }
        return object
    }
    private static func finite(_ value: Any) -> Bool {
        if let number = value as? NSNumber { return number.doubleValue.isFinite }
        if let object = value as? [String: Any] { return object.values.allSatisfy(finite) }
        if let array = value as? [Any] { return array.allSatisfy(finite) }
        return true
    }
    private struct Scanner {
        let bytes: [UInt8]
        var index = 0
        mutating func space() { while index < bytes.count && [9,10,13,32].contains(bytes[index]) { index += 1 } }
        mutating func take(_ byte: UInt8) throws {
            space()
            guard index < bytes.count, bytes[index] == byte else { throw CompanionError.invalidProtocol }
            index += 1
        }
        mutating func string() throws -> String {
            space()
            let start = index
            try take(34)
            while index < bytes.count {
                let c = bytes[index]
                index += 1
                if c == 34 {
                    guard let string = try JSONSerialization.jsonObject(with: Data(bytes[start..<index]), options: .fragmentsAllowed) as? String else { throw CompanionError.invalidProtocol }
                    return string
                }
                if c == 92 { index += 1 }
                else if c < 32 { throw CompanionError.invalidProtocol }
            }
            throw CompanionError.invalidProtocol
        }
        mutating func value(depth: Int) throws {
            space()
            guard depth <= 32, index < bytes.count else { throw CompanionError.invalidProtocol }
            switch bytes[index] {
            case 123:
                index += 1; space()
                if index < bytes.count && bytes[index] == 125 { index += 1; return }
                var keys: Set<String> = []
                while true {
                    let key = try string()
                    guard keys.insert(key).inserted else { throw CompanionError.invalidProtocol }
                    try take(58); try value(depth: depth + 1); space()
                    guard index < bytes.count else { throw CompanionError.invalidProtocol }
                    if bytes[index] == 125 { index += 1; return }
                    try take(44)
                }
            case 91:
                index += 1; space()
                if index < bytes.count && bytes[index] == 93 { index += 1; return }
                while true {
                    try value(depth: depth + 1); space()
                    guard index < bytes.count else { throw CompanionError.invalidProtocol }
                    if bytes[index] == 93 { index += 1; return }
                    try take(44)
                }
            case 34: _ = try string()
            default:
                let start = index
                while index < bytes.count && ![9,10,13,32,44,93,125].contains(bytes[index]) { index += 1 }
                guard index > start else { throw CompanionError.invalidProtocol }
                let token = Data(bytes[start..<index])
                let scalar = try JSONSerialization.jsonObject(with: token, options: .fragmentsAllowed)
                if let number = scalar as? NSNumber, !number.doubleValue.isFinite { throw CompanionError.invalidProtocol }
            }
        }
    }
}

/// A receipt written to the pipe is not yet a committed worker tick. Status
/// messages are ordered after send requests; require a terminal status after
/// receipt delivery, without waiting for subsequently queued hook revisions.
struct WorkerReceiptDrain {
    private(set) var isPending = false
    private var receiptDelivered = false
    mutating func begin() { isPending = true; receiptDelivered = false }
    mutating func deliveredReceipt() { receiptDelivered = true }
    mutating func status(_ value: String) {
        if receiptDelivered && ["sent", "unchanged", "retrying", "blocked", "paused"].contains(value) {
            isPending = false
        }
    }
}

struct RunGate {
    var enabled = false
    var bound = false
    var setupReady = false
    var inspectionUnavailable = false
    var paused = false
    var sleeping = false
    var stopping = false
    var mayRun: Bool { enabled && bound && setupReady && !paused && !sleeping && !stopping }
    // A send emitted just before pause may arrive after pause was requested.
    // Keep that revision retryable; it is not evidence of a broken binding.
    var rejectedRequestCode: String {
        stopping || (enabled && bound && (inspectionUnavailable || (paused && setupReady))) ? "send_failed" : "device_not_configured"
    }
}

enum WorkerStartDecision {
    static func allowed(ownerExists: Bool, gate: RunGate, workerRunning: Bool,
                        bluetoothQuiescent: Bool, protocolBlocked: Bool) -> Bool {
        ownerExists && gate.mayRun && !workerRunning && bluetoothQuiescent && !protocolBlocked
    }
}

struct RestartPolicy {
    private var failures = 0
    mutating func delay(processExited: Bool, bleQuiescent: Bool, allowed: Bool) -> TimeInterval? {
        guard processExited, bleQuiescent, allowed else { return nil }
        let delay = [5.0, 15, 30, 60][min(failures, 3)]
        failures = min(failures + 1, 3)
        return delay
    }
    mutating func reset() { failures = 0 }
}

enum ResumeDecision {
    static func command(ready: Bool, resumed: Bool, allowed: Bool, explicitRecovery: Bool) -> String? {
        guard ready else { return nil }
        if !allowed { return resumed ? "pause" : nil }
        return !resumed || explicitRecovery ? "resume" : nil
    }
}

/// Pure BLE accounting. A disconnect timeout is failure, never evidence of quiescence.
struct BLETransfer {
    private var commands: [Data] = []
    private(set) var receipt = SendReceipt()
    private(set) var active = false
    private(set) var quiescent = true
    private(set) var needsDisconnect = false
    var nextPacket: Data? {
        guard active, !needsDisconnect, receipt.errorCode == nil, receipt.packets < commands.count else { return nil }
        return commands[receipt.packets]
    }
    mutating func begin(commands: [Data]) throws {
        guard quiescent, !active, commands.count == 129, commands.reduce(0, { $0 + $1.count }) == 30_511 else { throw CompanionError.invalidProtocol }
        self.commands = commands
        receipt = SendReceipt()
        active = true
        quiescent = false
        needsDisconnect = false
    }
    mutating func wrote(error: Bool) {
        guard let packet = nextPacket else { return }
        if error { cancel(code: "send_failed"); return }
        receipt.packets += 1
        receipt.bytes += packet.count
        if receipt.packets == commands.count { needsDisconnect = true }
    }
    mutating func cancel(code: String) {
        guard active else { return }
        receipt.errorCode = receipt.errorCode ?? code
        needsDisconnect = true
    }
    mutating func disconnected(error: Bool) {
        guard active else { return }
        if !needsDisconnect || error { receipt.errorCode = receipt.errorCode ?? "send_failed" }
        receipt.disconnected = true
        quiescent = true
        active = false
    }
    mutating func cleanupTimedOut() { cancel(code: "send_timeout") }
}
