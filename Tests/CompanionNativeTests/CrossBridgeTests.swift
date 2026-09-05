import Foundation
import CryptoKit
import EInkCore

/// Real ProcessHost -> real bridge -> real validated renderer -> temporary Codex
/// stdio fixture. Only the physical radio is replaced, by BLETransfer callbacks.
@MainActor
final class CrossBridgeTests: NativeTestCase {
    private func fixture() throws -> (CompanionOptions, CompanionSettings) {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let temp = FileManager.default.temporaryDirectory.appendingPathComponent("eink-cross-bridge-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
        addTeardownBlock { try FileManager.default.removeItem(at: temp) }
        let codex = temp.appendingPathComponent("codex.py")
        try FileManager.default.copyItem(at: root.appendingPathComponent("Tests/CompanionNativeTests/fixtures/codex.py"), to: codex)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: codex.path)
        let python = ProcessInfo.processInfo.environment["COMPANION_TEST_PYTHON"] ?? "/usr/bin/python3"
        let options = CompanionOptions(stateDirectory: temp.appendingPathComponent("state"), runtimeRoot: root)
        return (options, CompanionSettings(codexBinary: codex.path, pythonBinary: python))
    }

    func testRealBridgeStartsPausedThenCommitsOnlyCompleteMockBLEReceipt() async throws {
        let (options, settings) = try fixture()
        let probe = CrossBridgeProbe(options: options, settings: settings)
        try probe.start()
        await probe.wait { probe.ready }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(probe.sendCount, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: options.stateDirectory.appendingPathComponent("sync-journal.json").path))
        try probe.host.control("resume")
        await probe.wait { probe.observedStatuses.contains("sent") }
        await probe.stop()
        XCTAssertEqual(probe.sendCount, 1)
        XCTAssertEqual(probe.packetCount, 129)
        XCTAssertEqual(probe.byteCount, 30_511)
        XCTAssertNotNil(probe.dataReadAt)
        XCTAssertEqual(probe.acknowledgedBeforeReply, 0)
        let journal = try probe.journal()
        XCTAssertEqual(journal["requested_revision"] as? Int, journal["acknowledged_revision"] as? Int)
        // Resume and initial source reconciliation can each register a revision.
        // The contract is that all registered revisions, not an invented fixed
        // count, are acknowledged only after the single completed frame.
        XCTAssertTrue((journal["acknowledged_revision"] as? Int ?? 0) > 0)
        XCTAssertNotNil(journal["last_sent_at"] as? Double)
        XCTAssertEqual(journal["last_sent_frame_hash"] as? String, probe.rgbDigest)
        let calls = try String(contentsOf: URL(fileURLWithPath: settings.codexBinary).deletingPathExtension().appendingPathExtension("calls"), encoding: .utf8)
        XCTAssertEqual(calls.split(separator: "\n").map(String.init), ["initialize", "thread/list", "account/rateLimits/read", "account/usage/read", "account/read"])
    }

    func testRealBridgeAutomaticRestartPreservesPermanentErrorUntilExplicitRetry() async throws {
        let (options, settings) = try fixture()
        let failed = CrossBridgeProbe(options: options, settings: settings, radioError: "permission_denied")
        try failed.start()
        await failed.wait { failed.ready }
        try failed.host.control("resume")
        await failed.wait { failed.observedStatuses.contains("blocked") }
        await failed.stop()
        let first = try failed.journal()
        XCTAssertEqual(first["acknowledged_revision"] as? Int, 0)
        XCTAssertEqual(first["failure_count"] as? Int, 1)
        XCTAssertTrue(first["retry_at"] is NSNull)
        XCTAssertEqual(first["last_error_code"] as? String, "permission_denied")

        let restarted = CrossBridgeProbe(options: options, settings: settings)
        XCTAssertFalse(failed.host.isRunning)
        try restarted.start()
        XCTAssertFalse(restarted.sessionID == failed.sessionID)
        await restarted.wait { restarted.ready }
        // Same default argument used by automatic ready->resume in the app.
        try restarted.host.control("resume")
        await restarted.wait { restarted.observedStatuses.contains("blocked") }
        XCTAssertEqual(restarted.sendCount, 0)
        let preserved = try restarted.journal()
        XCTAssertEqual(preserved["failure_count"] as? Int, 1)
        XCTAssertEqual(preserved["last_error_code"] as? String, "permission_denied")
        XCTAssertEqual(preserved["acknowledged_revision"] as? Int, 0)

        try restarted.host.control("resume", retry: true)
        await restarted.wait { restarted.observedStatuses.contains("sent") }
        await restarted.stop()
        XCTAssertEqual(restarted.sendCount, 1)
        let recovered = try restarted.journal()
        XCTAssertEqual(recovered["requested_revision"] as? Int, recovered["acknowledged_revision"] as? Int)
        XCTAssertEqual(recovered["failure_count"] as? Int, 0)
        XCTAssertTrue(recovered["last_error_code"] is NSNull)
    }
}

@MainActor
private final class CrossBridgeProbe {
    let host = WorkerHost()
    let options: CompanionOptions
    let settings: CompanionSettings
    let sessionID = UUID().uuidString
    let radioError: String?
    var ready = false
    var status: String?
    var observedStatuses: Set<String> = []
    var statusError: String?
    var dataReadAt: Double?
    var sendCount = 0
    var packetCount = 0
    var byteCount = 0
    var rgbDigest: String?
    var acknowledgedBeforeReply: Int?
    var failure: String?
    private var correlation: SessionGuard

    init(options: CompanionOptions, settings: CompanionSettings, radioError: String? = nil) {
        self.options = options; self.settings = settings; self.radioError = radioError
        correlation = SessionGuard(sessionID: sessionID)
        host.onMessage = { [weak self] in self?.receive($0) }
        host.onFault = { [weak self] in self?.failure = $0 }
    }
    func start() throws {
        try host.start(executable: settings.pythonBinary, arguments: options.bridgeArguments(settings: settings, sessionID: sessionID), sessionID: sessionID)
    }
    func wait(_ predicate: () -> Bool) async {
        let deadline = Date().addingTimeInterval(8)
        while !predicate() && failure == nil && Date() < deadline { try? await Task.sleep(for: .milliseconds(20)) }
        XCTAssertNil(failure)
        if !predicate() { XCTFail("bridge timeout: ready=\(ready), status=\(status ?? "none"), code=\(statusError ?? "none"), sends=\(sendCount), running=\(host.isRunning), state=\(options.stateDirectory.path)") }
    }
    func stop() async { host.stop(); await wait { !host.isRunning } }
    func journal() throws -> [String: Any] {
        try StrictJSON.object(Data(contentsOf: options.stateDirectory.appendingPathComponent("sync-journal.json")))
    }
    private func receive(_ message: WorkerMessage) {
        do {
            switch message {
            case .ready(let session):
                try correlation.ready(sessionID: session); ready = true
            case .status(let session, let payload):
                XCTAssertEqual(session, sessionID)
                status = payload["status"]?.string
                if let status { observedStatuses.insert(status) }
                statusError = payload["error_code"]?.string
                if let stamp = payload["data_read_at"]?.number { dataReadAt = stamp }
            case .send(let session, let request, let png, let hash):
                try correlation.begin(requestID: request, sessionID: session)
                sendCount += 1
                let frame = try StrictPNG.decode(png, sha256: hash)
                let commands = try EPDProtocol.commands(planes: frame.bitplanes(), chunkSize: 240)
                var transfer = BLETransfer()
                try transfer.begin(commands: commands)
                var rgb = Data()
                for pixel in frame.pixels {
                    switch pixel {
                    case .white: rgb.append(contentsOf: [255,255,255])
                    case .black: rgb.append(contentsOf: [0,0,0])
                    case .red: rgb.append(contentsOf: [198,40,40])
                    }
                }
                rgbDigest = SHA256.hash(data: rgb).map { String(format: "%02x", $0) }.joined()
                if let radioError { transfer.cancel(code: radioError) }
                else {
                    while transfer.nextPacket != nil { transfer.wrote(error: false) }
                    XCTAssertFalse(transfer.receipt.ok) // Writes alone cannot acknowledge.
                }
                acknowledgedBeforeReply = try journal()["acknowledged_revision"] as? Int
                transfer.disconnected(error: false) // Mock radio's final callback.
                packetCount = transfer.receipt.packets; byteCount = transfer.receipt.bytes
                XCTAssertTrue(correlation.complete(requestID: request, sessionID: session))
                try host.sendResult(sessionID: session, requestID: request, receipt: transfer.receipt)
            }
        } catch { failure = String(describing: error); host.stop() }
    }
}
