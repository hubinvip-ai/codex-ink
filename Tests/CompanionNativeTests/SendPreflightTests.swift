import Foundation

@MainActor
final class SendPreflightTests: NativeTestCase {
    func testTransientInspectionFailuresRetryButConflictsRemainBlocked() async {
        let request = identity()
        for (blockers, expected) in [(["process_inspection_failed"], "send_failed"),
                                     (["source_api_unavailable"], "send_failed"),
                                     (["config_changed"], "send_failed"),
                                     (["owned_hook_conflict"], "device_not_configured"),
                                     (["process_inspection_failed", "owned_hook_conflict"], "device_not_configured")] {
            var sends = 0
            let result = await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
                currentIdentity: { request }, mayRun: { true }, helperBusy: { false },
                inspect: { SetupInspection(ok: false, stage: "blocked", blockers: blockers, installationID: "installation", legacyPresent: false) },
                clock: { 101 }, send: { _, _ in sends += 1 })
            if case .rejected(let code, _, _) = result { XCTAssertEqual(code, expected) }
            else { XCTFail("must reject unavailable inspection") }
            XCTAssertEqual(sends, 0)
        }
    }

    private func identity() -> SendIdentity {
        SendIdentity(sessionID: "session", requestID: "frame", bindingRevision: 1, deviceIdentifier: UUID(), maintenanceEpoch: UUID())
    }
    private func inspection(ready: Bool = true, installation: String = "installation") -> SetupInspection {
        SetupInspection(ok: ready, stage: ready ? "ready" : "blocked", blockers: ready ? [] : ["owned_hook_conflict"], installationID: installation, legacyPresent: !ready)
    }

    func testChangedSetupAfterCachedReadyPreventsBLEStartUsingFreshRealPipeInspection() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("eink-preflight-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let record = directory.appendingPathComponent("current-definition.json")
        let runner = HelperRunner()
        let inspect: () async throws -> SetupInspection = {
            let result = try await runner.run(executable: "/bin/cat", arguments: [record.path], timeout: 2, limit: 65_536)
            guard result.status == 0 else { throw CompanionError.invalidProtocol }
            return try SetupInspection.decode(result.stdout)
        }
        try Data(#"{"ok":true,"stage":"ready","blockers":[],"installation_id":"installation","legacy_present":false}"#.utf8).write(to: record)
        let cached = try await inspect()
        XCTAssertTrue(cached.isReady)
        // Local stand-in for a definition being restored after startup inspection.
        try Data(#"{"ok":false,"stage":"blocked","blockers":["owned_hook_conflict"],"installation_id":"installation","legacy_present":true}"#.utf8).write(to: record, options: .atomic)
        let request = identity()
        var sends = 0
        let result = await SendPreflight.authorize(identity: request, installationID: cached.installationID, startedAt: 100,
            currentIdentity: { request }, mayRun: { true }, helperBusy: { runner.isRunning }, inspect: inspect, clock: { 101 }, send: { _, _ in sends += 1 })
        XCTAssertEqual(sends, 0)
        if case .rejected(let code, _, let checked) = result {
            XCTAssertEqual(code, "device_not_configured")
            XCTAssertFalse(checked?.isReady ?? true)
        } else { XCTFail("A cached ready cannot authorize changed global definitions") }
    }

    func testBusyOrFailedInspectorNeverFallsBackToCachedReady() async {
        let request = identity()
        var calls = 0
        var sends = 0
        let busy = await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
            currentIdentity: { request }, mayRun: { true }, helperBusy: { true }, inspect: { calls += 1; return self.inspection() }, clock: { 100 }, send: { _, _ in sends += 1 })
        if case .rejected(let code, _, _) = busy { XCTAssertEqual(code, "send_failed") }
        else { XCTFail("Busy inspector must defer") }
        XCTAssertEqual(calls, 0)
        let failed = await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
            currentIdentity: { request }, mayRun: { true }, helperBusy: { false }, inspect: { throw CompanionError.invalidProtocol }, clock: { 100 }, send: { _, _ in sends += 1 })
        if case .rejected(let code, _, _) = failed { XCTAssertEqual(code, "configuration_error") }
        else { XCTFail("Failed inspector must reject") }
        XCTAssertEqual(sends, 0)
    }

    func testSessionRequestBindingAndPauseAreRecheckedAfterAwait() async {
        for change in ["session", "request", "revision", "device", "epoch", "invalidated", "restore", "sleep", "pause"] {
            let request = identity()
            var current: SendIdentity? = request
            var allowed = true
            var sends = 0
            let result = await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
                currentIdentity: { current }, mayRun: { allowed }, helperBusy: { false }, inspect: {
                    await Task.yield()
                    switch change {
                    case "session": current?.sessionID = "new"
                    case "request": current?.requestID = "new"
                    case "revision": current?.bindingRevision += 1
                    case "device": current?.deviceIdentifier = UUID()
                    case "epoch", "restore", "sleep": current?.maintenanceEpoch = UUID()
                    case "invalidated": current = nil
                    default: allowed = false
                    }
                    return self.inspection()
                }, clock: { 101 }, send: { _, _ in sends += 1 })
            XCTAssertEqual(sends, 0)
            if change == "pause" {
                if case .rejected(let code, _, _) = result { XCTAssertEqual(code, "send_failed") }
                else { XCTFail("Pause during inspection must not start BLE") }
            } else if case .superseded = result { /* correct */ }
            else { XCTFail("Changed \(change) must invalidate the pending send") }
        }
    }

    func testTimedOutRealInspectorProcessCannotStartBLE() async {
        let request = identity()
        let runner = HelperRunner()
        var sends = 0
        let result = await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
            currentIdentity: { request }, mayRun: { true }, helperBusy: { runner.isRunning }, inspect: {
                _ = try await runner.run(executable: "/bin/sleep", arguments: ["2"], timeout: 0.05)
                return self.inspection()
            }, clock: { 101 }, send: { _, _ in sends += 1 })
        if case .rejected(let code, _, _) = result { XCTAssertEqual(code, "send_failed") }
        else { XCTFail("Timed-out authoritative inspection must reject") }
        XCTAssertEqual(sends, 0)
        XCTAssertFalse(runner.isRunning)
    }

    func testReadOnlyInspectorTimeoutIsBoundedWhenDescendantHoldsStdout() async {
        let request = identity()
        let runner = HelperRunner()
        var sends = 0
        let script = "import os,time\nif os.fork()==0:\n time.sleep(3)\n os._exit(0)\nos._exit(0)"
        let began = Date()
        let result = await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
            currentIdentity: { request }, mayRun: { true }, helperBusy: { runner.isRunning }, inspect: {
                _ = try await runner.run(executable: "/usr/bin/python3", arguments: ["-B", "-c", script], timeout: 0.1, readOnly: true)
                return self.inspection()
            }, clock: { 101 }, send: { _, _ in sends += 1 })
        XCTAssertTrue(Date().timeIntervalSince(began) < 1)
        if case .rejected(let code, _, _) = result { XCTAssertEqual(code, "send_failed") }
        else { XCTFail("Retained descendant stdout must not authorize a frame") }
        XCTAssertEqual(sends, 0)
        XCTAssertFalse(runner.isRunning)
    }

    func testFreshInspectionBindsInstallationAndPreservesBridgeDeadline() async {
        let request = identity()
        var sends = 0
        func authorize(_ now: Double, installation: String = "installation") async -> SendPreflight.Result {
            await SendPreflight.authorize(identity: request, installationID: "installation", startedAt: 100,
                currentIdentity: { request }, mayRun: { true }, helperBusy: { false }, inspect: { self.inspection(installation: installation) }, clock: { now }, send: { _, _ in sends += 1 })
        }
        if case .allowed(_, let seconds) = await authorize(110) { XCTAssertEqual(seconds, 114) }
        else { XCTFail("Fresh matching ready should authorize remaining BLE budget") }
        if case .rejected(let code, _, _) = await authorize(225) { XCTAssertEqual(code, "send_timeout") }
        else { XCTFail("Expired bridge request must not start BLE") }
        if case .rejected(let code, _, _) = await authorize(101, installation: "new-installation") { XCTAssertEqual(code, "device_not_configured") }
        else { XCTFail("A different installation cannot authorize the old request") }
        XCTAssertEqual(sends, 1)
    }
}
