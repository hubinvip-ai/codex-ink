import Foundation

@MainActor final class RuntimeRoutingTests: NativeTestCase {
    private func resources() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("runtime-routing-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("runtime"), withIntermediateDirectories: true)
        addTeardownBlock { try FileManager.default.removeItem(at: root) }
        return root
    }
    func testStoppingRejectsLateSendAsRetryable() {
        let gate = RunGate(enabled: true, bound: true, setupReady: true, stopping: true)
        XCTAssertFalse(gate.mayRun)
        XCTAssertEqual(gate.rejectedRequestCode, "send_failed")
    }

    private let settings = CompanionSettings(codexBinary: "/usr/bin/codex", pythonBinary: "/usr/bin/python3")

    func testNewPackageRoutesSyncAndFrozenHooksSeparately() throws {
        let root = try resources()
        let sync = root.appendingPathComponent("sync-runtime")
        let hooks = root.appendingPathComponent("runtime")
        try FileManager.default.createDirectory(at: sync, withIntermediateDirectories: true)
        let options = try CompanionOptions.parse(["--state-dir", "/isolated/state"], resources: root)
        XCTAssertEqual(options.bridgeArguments(settings: settings, sessionID: "session").first, sync.appendingPathComponent("tools/companion_bridge.py").path)
        XCTAssertEqual(options.previewArguments(settings: settings).first, sync.appendingPathComponent("tools/codex_eink_reliable.py").path)
        for action in ["inspect", "install", "restore"] {
            let args = options.setupArguments(action: action, settings: settings)
            XCTAssertEqual(args.first, hooks.appendingPathComponent("tools/companion_setup.py").path)
            if action == "install" { XCTAssertEqual(Array(args.suffix(2)), ["--runtime-root", hooks.path]) }
        }
    }

    func testLegacyPackageKeepsSingleRuntime() throws {
        let root = try resources()
        let runtime = root.appendingPathComponent("runtime")
        let options = try CompanionOptions.parse(["--state-dir", "/isolated/state"], resources: root)
        XCTAssertEqual(options.runtimeRoot, runtime)
        XCTAssertEqual(options.bridgeArguments(settings: settings, sessionID: "session").first, runtime.appendingPathComponent("tools/companion_bridge.py").path)
        XCTAssertEqual(options.previewArguments(settings: settings).first, runtime.appendingPathComponent("tools/codex_eink_reliable.py").path)
        XCTAssertEqual(options.setupArguments(action: "inspect", settings: settings).first, runtime.appendingPathComponent("tools/companion_setup.py").path)
    }

    func testExplicitRuntimeOverridesBothBundledDirectories() throws {
        let root = try resources()
        try FileManager.default.createDirectory(at: root.appendingPathComponent("sync-runtime"), withIntermediateDirectories: true)
        let explicit = "/development/runtime"
        let options = try CompanionOptions.parse(["--state-dir", "/isolated/state", "--runtime-root", explicit], resources: root)
        XCTAssertEqual(options.bridgeArguments(settings: settings, sessionID: "session").first, explicit + "/tools/companion_bridge.py")
        XCTAssertEqual(options.previewArguments(settings: settings).first, explicit + "/tools/codex_eink_reliable.py")
        for action in ["inspect", "install", "restore"] {
            XCTAssertEqual(options.setupArguments(action: action, settings: settings).first, explicit + "/tools/companion_setup.py")
        }
        XCTAssertEqual(Array(options.setupArguments(action: "install", settings: settings).suffix(2)), ["--runtime-root", explicit])
    }
}
