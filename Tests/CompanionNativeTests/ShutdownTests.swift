import Foundation

@MainActor final class ShutdownTests: NativeTestCase {
    func testReceiptNeedsSubsequentTerminalStatusToSettle() {
        var drain = WorkerReceiptDrain()
        XCTAssertFalse(drain.isPending)
        drain.begin()
        drain.status("sent")
        XCTAssertTrue(drain.isPending)
        drain.deliveredReceipt()
        drain.status("pending")
        XCTAssertTrue(drain.isPending)
        drain.status("sent")
        XCTAssertFalse(drain.isPending)
    }

    func testFailureOrPauseSettlesOnlyAfterReceiptAndNewRequestResets() {
        for status in ["unchanged", "retrying", "blocked", "paused"] {
            var drain = WorkerReceiptDrain()
            drain.begin()
            drain.deliveredReceipt()
            drain.status(status)
            XCTAssertFalse(drain.isPending)
            drain.begin()
            drain.status(status)
            XCTAssertTrue(drain.isPending)
        }
    }

    func testShutdownPausesWorkerBeforeStopWithoutChangingSavedPause() async throws {
        try await exerciseShutdown(waitForReceipt: false)
    }

    func testShutdownWaitsForWorkerToSettleDeliveredReceiptBeforeStop() async throws {
        try await exerciseShutdown(waitForReceipt: true)
    }

    private func exerciseShutdown(waitForReceipt: Bool) async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("shutdown-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let tools = root.appendingPathComponent("tools"), state = root.appendingPathComponent("state")
        try FileManager.default.createDirectory(at: tools, withIntermediateDirectories: true)
        let helper = """
        import json
        print(json.dumps({'ok':True,'stage':'ready','blockers':[],'installation_id':'test-install','legacy_present':False}))
        """
        try helper.write(to: tools.appendingPathComponent("companion_setup.py"), atomically: true, encoding: .utf8)
        let worker = """
        import json,sys,select
        from pathlib import Path
        drain_mode=\(waitForReceipt ? "True" : "False")
        directory=Path(__file__).parent
        session=sys.argv[sys.argv.index('--session-id')+1]
        print(json.dumps({'version':1,'type':'ready','session_id':session}),flush=True)
        for line in sys.stdin:
            with Path(__file__).parent.joinpath('commands.jsonl').open('a') as output: output.write(line)
            kind=json.loads(line)['type']
            if kind=='stop': break
            if drain_mode and kind=='resume':
                print(json.dumps({'version':1,'type':'send','session_id':session,'request_id':'pending','png_base64':'AQ==','png_sha256':'0'*64}),flush=True)
            if drain_mode and kind=='send_result': directory.joinpath('receipt-seen').touch()
            if drain_mode and kind=='pause':
                # A stop readable before terminal status exposes the receipt /
                # journal race, even if the parent later waits for process exit.
                if select.select([sys.stdin],[],[],0.3)[0]:
                    directory.joinpath('premature-stop').touch()
                    break
                print(json.dumps({'version':1,'type':'status','session_id':session,'payload':{'status':'retrying','error_code':'invalid_frame'}}),flush=True)
        """
        try worker.write(to: tools.appendingPathComponent("companion_bridge.py"), atomically: true, encoding: .utf8)
        var settings = CompanionSettings(codexBinary: "/usr/bin/true", pythonBinary: ProcessInfo.processInfo.environment["COMPANION_TEST_PYTHON"]!)
        try settings.bind(identifier: UUID(), name: "test screen", gattValidated: true, quiescent: true)
        settings.syncEnabled = true
        try SettingsStore(directory: state).save(settings)
        let model = CompanionModel(arguments: ["--state-dir", state.path, "--runtime-root", root.path])
        await model.start()
        for _ in 0..<30 {
            if model.workerReady { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertTrue(model.workerReady)
        if waitForReceipt {
            for _ in 0..<30 {
                if FileManager.default.fileExists(atPath: tools.appendingPathComponent("receipt-seen").path) { break }
                try await Task.sleep(for: .milliseconds(100))
            }
            XCTAssertTrue(FileManager.default.fileExists(atPath: tools.appendingPathComponent("receipt-seen").path))
        }
        let clean = await model.shutdown()
        XCTAssertTrue(clean)
        XCTAssertFalse(FileManager.default.fileExists(atPath: tools.appendingPathComponent("premature-stop").path))
        let lines = try String(contentsOf: tools.appendingPathComponent("commands.jsonl"), encoding: .utf8).split(separator: "\n")
        let types = try lines.map { try StrictJSON.object(Data($0.utf8))["type"] as? String }
        XCTAssertEqual(types.suffix(2).compactMap { $0 }, ["pause", "stop"])
        XCTAssertFalse(try XCTUnwrap(SettingsStore(directory: state).load()).paused)
    }
}
