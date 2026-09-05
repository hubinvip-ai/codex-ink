import Foundation

@MainActor
final class RecoveryTests: NativeTestCase {
    func testPeriodicInspectionRecoversWithoutManualRetryAndPauseActionResumes() async throws {
        try await exerciseRecovery(initiallyPaused: false)
    }

    func testPeriodicRecoveryPreservesPauseUntilUserResumes() async throws {
        try await exerciseRecovery(initiallyPaused: true)
    }

    private func exerciseRecovery(initiallyPaused: Bool) async throws {
        let home = FileManager.default.homeDirectoryForCurrentUser
        XCTAssertTrue(home.path.contains("eink-native-tests-"))
        let root = home.appendingPathComponent("recovery-" + UUID().uuidString)
        let tools = root.appendingPathComponent("tools")
        let state = root.appendingPathComponent("state")
        try FileManager.default.createDirectory(at: tools, withIntermediateDirectories: true)
        let helper = """
        import json,sys
        from pathlib import Path
        flag=Path(__file__).parent/'checked'
        first=not flag.exists()
        flag.touch()
        print(json.dumps({'ok':not first,'stage':'blocked' if first else 'ready','blockers':['process_inspection_failed'] if first else [],'installation_id':'test-install','legacy_present':False}))
        sys.exit(1 if first else 0)
        """
        try helper.write(to: tools.appendingPathComponent("companion_setup.py"), atomically: true, encoding: .utf8)
        let worker = """
        import json,sys
        from pathlib import Path
        Path(__file__).parent.joinpath('worker-started').touch()
        session=sys.argv[sys.argv.index('--session-id')+1]
        print(json.dumps({'version':1,'type':'ready','session_id':session}),flush=True)
        for line in sys.stdin:
            message=json.loads(line)
            if message['type']=='stop': break
            Path(__file__).parent.joinpath('commands.jsonl').open('a').write(line)
        """
        try worker.write(to: tools.appendingPathComponent("companion_bridge.py"), atomically: true, encoding: .utf8)
        var settings = CompanionSettings(codexBinary: "/usr/bin/true", pythonBinary: ProcessInfo.processInfo.environment["COMPANION_TEST_PYTHON"]!)
        try settings.bind(identifier: UUID(), name: "test screen", gattValidated: true, quiescent: true)
        settings.syncEnabled = true
        settings.paused = initiallyPaused
        try SettingsStore(directory: state).save(settings)
        let model = CompanionModel(arguments: ["--state-dir", state.path, "--runtime-root", root.path])
        await model.start()
        XCTAssertFalse(model.workerRunning)
        XCTAssertNotNil(model.errorMessage)
        // Exercise the real 15-second polling path, not a hand-called helper.
        for _ in 0..<190 {
            if model.setup?.isReady == true && (initiallyPaused || model.workerReady) { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertTrue(model.setup?.isReady == true)
        XCTAssertNil(model.errorMessage)
        if initiallyPaused {
            XCTAssertFalse(model.workerRunning)
            XCTAssertFalse(FileManager.default.fileExists(atPath: tools.appendingPathComponent("worker-started").path))
            XCTAssertEqual(model.statusLabel, "已暂停")
        } else {
            XCTAssertTrue(model.workerReady)
            XCTAssertEqual(model.statusLabel, "就绪")
            await model.setPaused(true)
        }
        XCTAssertEqual(model.primaryLabel, "继续同步")
        _ = await model.performPrimaryAction()
        XCTAssertFalse(model.settings.paused)
        XCTAssertNil(model.errorMessage)
        for _ in 0..<30 {
            if FileManager.default.fileExists(atPath: tools.appendingPathComponent("commands.jsonl").path) { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        // Poll recovery must not grant retry:true for permanent failures.
        let commands = try String(contentsOf: tools.appendingPathComponent("commands.jsonl"), encoding: .utf8)
        XCTAssertFalse(commands.contains("\"retry\":true"))
        let stopped = await model.shutdown()
        XCTAssertTrue(stopped)
    }
}
