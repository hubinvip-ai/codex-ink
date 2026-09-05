import Foundation
import CryptoKit
import EInkCore

@MainActor
final class ContractTests: NativeTestCase {
    func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("companion-tests-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testFirstLaunchNeverEnablesSyncAndWritesExactSchemaIncludingNullBinding() throws {
        let store = SettingsStore(directory: try temporaryDirectory())
        XCTAssertNil(try store.load())
        let settings = CompanionSettings(codexBinary: "/Applications/Codex.app/Contents/Resources/codex", pythonBinary: "/usr/bin/python3")
        XCTAssertFalse(settings.syncEnabled)
        try store.save(settings)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: store.url)) as? [String: Any])
        XCTAssertEqual(Set(json.keys), ["version", "codex_binary", "python_binary", "device_identifier", "device_name", "binding_revision", "sync_enabled", "paused"])
        XCTAssertTrue(json["device_identifier"] is NSNull)
        XCTAssertEqual(try store.load(), settings)
    }

    func testCorruptUnknownVersionOrInconsistentSettingsAreNotOverwritten() throws {
        let store = SettingsStore(directory: try temporaryDirectory())
        for invalid in [Data("{bad".utf8), Data(#"{"version":2}"#.utf8)] {
            try invalid.write(to: store.url)
            XCTAssertThrowsError(try store.load())
            XCTAssertThrowsError(try store.save(CompanionSettings(codexBinary: "/a", pythonBinary: "/b")))
            XCTAssertEqual(try Data(contentsOf: store.url), invalid)
        }
        var unbound = CompanionSettings(codexBinary: "/a", pythonBinary: "/b")
        unbound.syncEnabled = true
        XCTAssertThrowsError(try unbound.validated())
    }

    func testOnlyQuiescentValidatedBindingCanIncreaseRevision() throws {
        var settings = CompanionSettings(codexBinary: "/a", pythonBinary: "/b")
        let id = UUID()
        XCTAssertThrowsError(try settings.bind(identifier: id, name: "screen", gattValidated: false, quiescent: true))
        XCTAssertThrowsError(try settings.bind(identifier: id, name: "screen", gattValidated: true, quiescent: false))
        try settings.bind(identifier: id, name: "screen", gattValidated: true, quiescent: true)
        XCTAssertEqual(settings.deviceIdentifier, id)
        XCTAssertEqual(settings.bindingRevision, 1)
        XCTAssertFalse(settings.syncEnabled)
    }

    func testSingleOwnerLockExcludesAnotherInstanceAndReleasesOnClose() throws {
        let url = try temporaryDirectory().appendingPathComponent("owner.lock")
        var owner: CompanionOwnerLock? = try CompanionOwnerLock(url: url)
        XCTAssertNotNil(owner)
        XCTAssertThrowsError(try CompanionOwnerLock(url: url))
        owner = nil
        XCTAssertNoThrow(try CompanionOwnerLock(url: url))
    }

    func testJSONLFragmentationAndOneMiBIncludingNewline() throws {
        var framer = JSONLFramer()
        XCTAssertEqual(try framer.append(Data("{\"a\":".utf8)), [])
        XCTAssertEqual(try framer.append(Data("1}\n{}\n".utf8)).count, 2)
        XCTAssertNoThrow(try framer.finish())
        var full = JSONLFramer()
        XCTAssertEqual(try full.append(Data(repeating: 0x20, count: 1_048_575)), [])
        XCTAssertEqual(try full.append(Data([10])).first?.count, 1_048_575)
        var tooLarge = JSONLFramer()
        XCTAssertThrowsError(try tooLarge.append(Data(repeating: 0x20, count: 1_048_576)))
        var partial = JSONLFramer()
        _ = try partial.append(Data("{}".utf8))
        XCTAssertThrowsError(try partial.finish())
    }

    func testProtocolRejectsWrongTypesExtraTargetAndUnknownVersion() throws {
        let session = UUID().uuidString
        func decode(_ extra: [String: Any]) throws -> WorkerMessage {
            var json: [String: Any] = ["version": 1, "type": "ready", "session_id": session]
            json.merge(extra) { _, new in new }
            return try WorkerMessage.decode(JSONSerialization.data(withJSONObject: json))
        }
        XCTAssertNoThrow(try decode([:]))
        XCTAssertThrowsError(try decode(["version": true]))
        XCTAssertThrowsError(try decode(["version": 2]))
        XCTAssertThrowsError(try decode(["device_identifier": UUID().uuidString]))
        XCTAssertThrowsError(try decode(["type": "other"]))
        XCTAssertThrowsError(try WorkerMessage.decode(Data([0xFF])))
    }

    func testProtocolRejectsDuplicateKeysDeepNestingAndNonFiniteNumbers() throws {
        for invalid in [
            #"{"version":1,"version":1,"type":"ready","session_id":"s"}"#,
            #"{"version":1,"type":"status","session_id":"s","payload":{"a":1,"\u0061":2}}"#,
            #"{"version":1,"type":"status","session_id":"s","payload":{"n":1e999}}"#,
            #"{"version":1.0,"type":"ready","session_id":"s"}"#,
            "{\"version\":1,\"type\":\"status\",\"session_id\":\"s\",\"payload\":{\"a\":" + String(repeating: "[", count: 64) + "0" + String(repeating: "]", count: 64) + "}}"
        ] { XCTAssertThrowsError(try WorkerMessage.decode(Data(invalid.utf8))) }
    }

    func testCorrelationRejectsConcurrentDuplicateAndStaleCallbacks() throws {
        var session = SessionGuard(sessionID: "session")
        XCTAssertThrowsError(try session.begin(requestID: "early", sessionID: "session"))
        try session.ready(sessionID: "session")
        try session.begin(requestID: "a", sessionID: "session")
        XCTAssertThrowsError(try session.begin(requestID: "b", sessionID: "session"))
        XCTAssertFalse(session.complete(requestID: "a", sessionID: "old"))
        XCTAssertFalse(session.complete(requestID: "b", sessionID: "session"))
        XCTAssertTrue(session.complete(requestID: "a", sessionID: "session"))
        XCTAssertFalse(session.complete(requestID: "a", sessionID: "session"))
        XCTAssertThrowsError(try session.begin(requestID: "a", sessionID: "session"))
    }

    func testSendResultUsesExactContractAndRequiresRealDisconnectEvidence() throws {
        let result = SendReceipt(packets: 129, bytes: 30_511, disconnected: true, errorCode: nil)
        let data = try BridgeOutput.result(sessionID: "s", requestID: "r", receipt: result)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(json.keys), ["version", "type", "session_id", "request_id", "ok", "packets", "bytes", "disconnected"])
        XCTAssertEqual(json["bytes"] as? Int, 30_511)
        for invalid in [SendReceipt(packets: 129, bytes: 30_511, disconnected: false, errorCode: nil), SendReceipt(packets: 128, bytes: 30_511, disconnected: true, errorCode: nil)] {
            let failed = try XCTUnwrap(JSONSerialization.jsonObject(with: BridgeOutput.result(sessionID: "s", requestID: "r", receipt: invalid)) as? [String: Any])
            XCTAssertEqual(failed["ok"] as? Bool, false)
            XCTAssertEqual(failed["error_code"] as? String, "send_failed")
            XCTAssertNil(failed["packets"])
        }
    }

    func testSetupReadinessCannotBeFakedByEnabledSettingsOrFailedInspector() throws {
        var gate = RunGate()
        gate.enabled = true
        gate.bound = true
        XCTAssertFalse(gate.mayRun)
        gate.setupReady = true
        XCTAssertTrue(gate.mayRun)
        gate.paused = true
        XCTAssertFalse(gate.mayRun)
        gate.paused = false
        gate.sleeping = true
        XCTAssertFalse(gate.mayRun)
        gate.sleeping = false
        gate.stopping = true
        XCTAssertFalse(gate.mayRun)
        let bad = Data(#"{"ok":false,"stage":"ready","blockers":[],"installation_id":null,"legacy_present":false}"#.utf8)
        XCTAssertFalse(try SetupInspection.decode(bad).isReady)
    }

    func testPauseRaceKeepsPendingRequestRetryableInsteadOfCreatingPermanentBindingFailure() {
        var gate = RunGate(enabled: true, bound: true, setupReady: true, paused: true)
        XCTAssertEqual(gate.rejectedRequestCode, "send_failed")
        gate.bound = false
        XCTAssertEqual(gate.rejectedRequestCode, "device_not_configured")
    }

    func testRequestAfterTransientInspectionPauseIsRetryable() {
        var gate = RunGate(enabled: true, bound: true, setupReady: false, inspectionUnavailable: true)
        XCTAssertEqual(gate.rejectedRequestCode, "send_failed")
        gate.inspectionUnavailable = false
        XCTAssertEqual(gate.rejectedRequestCode, "device_not_configured")
        gate.inspectionUnavailable = true; gate.bound = false
        XCTAssertEqual(gate.rejectedRequestCode, "device_not_configured")
    }

    func testWorkerNeverStartsBeforeTheCompleteSendGateIsOpen() {
        var gate = RunGate(enabled: true, bound: true, setupReady: true)
        XCTAssertTrue(WorkerStartDecision.allowed(ownerExists: true, gate: gate, workerRunning: false,
                                                  bluetoothQuiescent: true, protocolBlocked: false))
        gate.setupReady = false
        XCTAssertFalse(WorkerStartDecision.allowed(ownerExists: true, gate: gate, workerRunning: false,
                                                   bluetoothQuiescent: true, protocolBlocked: false))
        gate.setupReady = true; gate.enabled = false
        XCTAssertFalse(WorkerStartDecision.allowed(ownerExists: true, gate: gate, workerRunning: false,
                                                   bluetoothQuiescent: true, protocolBlocked: false))
        gate.enabled = true; gate.paused = true
        XCTAssertFalse(WorkerStartDecision.allowed(ownerExists: true, gate: gate, workerRunning: false,
                                                   bluetoothQuiescent: true, protocolBlocked: false))
    }

    func testIncompleteSetupNeverClaimsLegacyIsAbsent() throws {
        let cases: [(Bool, String, [String], Bool, String)] = [
            (false, "blocked", ["unknown_hook_source"], false, "旧方案检查尚未完成"),
            (false, "blocked", ["unknown_hook_source"], true, "旧方案检查尚未完成"),
            (true, "blocked", [], false, "旧方案检查尚未完成"),
            (false, "needs_install", [], false, "旧方案检查尚未完成"),
            (true, "needs_install", [], false, "未检测到已识别的旧方案配置。"),
            (true, "legacy_active", [], true, "检测到旧方案配置。"),
        ]
        for (ok, stage, blockers, present, expected) in cases {
            let data = try JSONSerialization.data(withJSONObject: ["ok": ok, "stage": stage, "blockers": blockers, "legacy_present": present, "installation_id": NSNull()] as [String: Any])
            XCTAssertEqual(try SetupInspection.decode(data).legacySummary, expected)
        }
    }

    func testWorkerRestartWaitsForBothProcessExitAndBluetoothQuiescence() {
        var restart = RestartPolicy()
        XCTAssertNil(restart.delay(processExited: true, bleQuiescent: false, allowed: true))
        XCTAssertNil(restart.delay(processExited: false, bleQuiescent: true, allowed: true))
        XCTAssertEqual((0..<6).map { _ in restart.delay(processExited: true, bleQuiescent: true, allowed: true)! }, [5, 15, 30, 60, 60, 60])
        XCTAssertNil(restart.delay(processExited: true, bleQuiescent: true, allowed: false))
    }

    func testExplicitRetryResumesEvenIfWorkerAlreadyRunningButAutomaticRestartCannotResetPermanentFailure() {
        XCTAssertEqual(ResumeDecision.command(ready: true, resumed: true, allowed: true, explicitRecovery: true), "resume")
        XCTAssertEqual(ResumeDecision.command(ready: true, resumed: false, allowed: true, explicitRecovery: false), "resume")
        XCTAssertNil(ResumeDecision.command(ready: false, resumed: false, allowed: true, explicitRecovery: true))
        XCTAssertEqual(ResumeDecision.command(ready: true, resumed: true, allowed: false, explicitRecovery: true), "pause")
        XCTAssertNil(ResumeDecision.command(ready: true, resumed: false, allowed: false, explicitRecovery: true))
        XCTAssertNil(ResumeDecision.command(ready: true, resumed: true, allowed: true, explicitRecovery: false))
    }

    func testResumeRetryIsAnExplicitBooleanNeverAddedToAutomaticResume() throws {
        let automatic = try XCTUnwrap(JSONSerialization.jsonObject(with: BridgeOutput.control("resume", sessionID: "s")) as? [String: Any])
        XCTAssertNil(automatic["retry"])
        let explicit = try XCTUnwrap(JSONSerialization.jsonObject(with: BridgeOutput.control("resume", sessionID: "s", retry: true)) as? [String: Any])
        XCTAssertEqual(explicit["retry"] as? Bool, true)
        XCTAssertThrowsError(try BridgeOutput.control("pause", sessionID: "s", retry: true))
    }

    func testDuplicateReadyAndOldSessionReadyAreRejected() throws {
        var session = SessionGuard(sessionID: "new")
        XCTAssertThrowsError(try session.ready(sessionID: "old"))
        try session.ready(sessionID: "new")
        XCTAssertThrowsError(try session.ready(sessionID: "new"))
    }

    func testSerialWriteCompletionNeedsAllCallbacksAndDisconnect() throws {
        var transfer = BLETransfer()
        let commands = try EPDProtocol.commands(planes: EInkFrame(width: 400, height: 300, pixels: Array(repeating: .white, count: 120_000)).bitplanes(), chunkSize: 240)
        try transfer.begin(commands: commands)
        XCTAssertThrowsError(try transfer.begin(commands: commands))
        for _ in 0..<129 {
            XCTAssertNotNil(transfer.nextPacket)
            transfer.wrote(error: false)
        }
        XCTAssertNil(transfer.nextPacket)
        XCTAssertFalse(transfer.receipt.ok)
        XCTAssertTrue(transfer.needsDisconnect)
        transfer.disconnected(error: false)
        XCTAssertTrue(transfer.receipt.ok)
        XCTAssertEqual(transfer.receipt.bytes, 30_511)
    }

    func testEarlyDisconnectAndCleanupTimeoutNeverBecomeSuccess() throws {
        var transfer = BLETransfer()
        let commands = try EPDProtocol.commands(planes: EInkFrame(width: 400, height: 300, pixels: Array(repeating: .white, count: 120_000)).bitplanes(), chunkSize: 240)
        try transfer.begin(commands: commands)
        transfer.wrote(error: false)
        transfer.cancel(code: "send_timeout")
        XCTAssertNil(transfer.nextPacket)
        transfer.cleanupTimedOut()
        XCTAssertFalse(transfer.quiescent)
        XCTAssertFalse(transfer.receipt.ok)
        transfer.disconnected(error: false)
        XCTAssertTrue(transfer.quiescent)
        XCTAssertFalse(transfer.receipt.ok)
    }
}
