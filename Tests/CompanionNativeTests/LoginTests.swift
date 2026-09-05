import ServiceManagement
import Foundation

@MainActor
final class LoginTests: NativeTestCase {
    private func isolatedStartupURL() -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        XCTAssertTrue(home.path.contains("eink-native-tests-"))
        return home.appendingPathComponent("Library/Application Support/CodexEInk/startup.json")
    }

    func testNoArgumentLaunchRestoresSelectedDirectoryAndSavedState() throws {
        let home = FileManager.default.homeDirectoryForCurrentUser
        // The Python runner isolates Foundation's home so no real user files are touched.
        XCTAssertTrue(home.path.contains("eink-native-tests-"))
        let base = home.appendingPathComponent("Library/Application Support/CodexEInk")
        let state = base.appendingPathComponent("chosen state")
        let startup = base.appendingPathComponent("startup.json")
        defer { try? FileManager.default.removeItem(at: startup) }
        var saved = CompanionSettings(codexBinary: "/fixture/codex", pythonBinary: "/fixture/python")
        try saved.bind(identifier: UUID(uuidString: "12345678-1234-1234-1234-123456789ABC")!, name: "saved screen", gattValidated: true, quiescent: true)
        saved.syncEnabled = true
        saved.paused = true
        try SettingsStore(directory: state).save(saved)
        let startupBytes = try JSONSerialization.data(withJSONObject: ["version": 1, "state_directory": state.path])
        try startupBytes.write(to: startup)
        let reopened = CompanionModel(arguments: [])
        XCTAssertTrue(reopened.configured)
        XCTAssertEqual(reopened.options?.stateDirectory.path, state.path)
        XCTAssertEqual(reopened.settings, saved)
        XCTAssertEqual(try Data(contentsOf: startup), startupBytes)
    }

    func testFreshInstallAndExplicitDirectoryDoNotChangeStartupSelection() throws {
        let startup = isolatedStartupURL()
        defer { try? FileManager.default.removeItem(at: startup) }
        let standard = startup.deletingLastPathComponent().appendingPathComponent("companion")
        XCTAssertEqual(try CompanionOptions.parse([]).stateDirectory.path, standard.path)
        XCTAssertFalse(FileManager.default.fileExists(atPath: startup.path))
        let corrupt = Data("{broken".utf8)
        try corrupt.write(to: startup)
        let explicit = try CompanionOptions.parse(["--state-dir", "/explicit state"])
        XCTAssertEqual(explicit.stateDirectory.path, "/explicit state")
        XCTAssertEqual(try Data(contentsOf: startup), corrupt)
    }

    func testDamagedSelectionAndMissingTargetBlockRecoveryWithoutOverwrite() throws {
        let startup = isolatedStartupURL()
        defer { try? FileManager.default.removeItem(at: startup) }
        for value in [
            #"{"version":2,"state_directory":"/missing"}"#,
            #"{"version":true,"state_directory":"/missing"}"#,
            #"{"version":1,"state_directory":"relative"}"#,
            #"{"version":1,"state_directory":"/missing","extra":1}"#,
            #"{"version":1,"state_directory":"/missing","version":1}"#,
            #"{"version":1,"state_directory":"/definitely-missing-codex-ink-state"}"#,
            "{broken"
        ] {
            let bytes = Data(value.utf8)
            try bytes.write(to: startup)
            XCTAssertThrowsError(try CompanionOptions.parse([]))
            let model = CompanionModel(arguments: [])
            XCTAssertFalse(model.configured)
            XCTAssertFalse(model.gate.mayRun)
            XCTAssertNotNil(model.errorMessage)
            XCTAssertEqual(try Data(contentsOf: startup), bytes)
        }
    }

    func testSavedSelectionRestoresUnpausedSettingsAndRejectsBadWrites() throws {
        let startup = isolatedStartupURL()
        defer { try? FileManager.default.removeItem(at: startup) }
        let state = startup.deletingLastPathComponent().appendingPathComponent("saved choice")
        var saved = CompanionSettings(codexBinary: "/fixture/codex", pythonBinary: "/fixture/python")
        try saved.bind(identifier: UUID(), name: "screen", gattValidated: true, quiescent: true)
        saved.syncEnabled = true
        try SettingsStore(directory: state).save(saved)
        let store = StartupStore(url: startup)
        try store.save(stateDirectory: state)
        let bytes = try Data(contentsOf: startup)
        let reopened = CompanionModel(arguments: [])
        XCTAssertEqual(reopened.settings, saved)
        XCTAssertFalse(reopened.settings.paused)
        XCTAssertThrowsError(try store.save(stateDirectory: state.appendingPathComponent("missing")))
        XCTAssertEqual(try Data(contentsOf: startup), bytes)
        let blocked = StartupStore(url: state) // An existing directory cannot become a file.
        XCTAssertThrowsError(try blocked.save(stateDirectory: state))
        let settingsURL = SettingsStore(directory: state).url
        let damaged = Data("{broken".utf8)
        try damaged.write(to: settingsURL)
        XCTAssertThrowsError(try CompanionOptions.parse([]))
        XCTAssertEqual(try Data(contentsOf: startup), bytes)
        XCTAssertEqual(try Data(contentsOf: settingsURL), damaged)
    }

    func testSystemStatusMappingSeparatesApprovalFromEnabledAndExternalDisable() {
        XCTAssertEqual(LoginItemState(.enabled), .enabled)
        XCTAssertEqual(LoginItemState(.requiresApproval), .requiresApproval)
        XCTAssertEqual(LoginItemState(.notRegistered), .notRegistered)
        XCTAssertEqual(LoginItemState(.notFound), .unavailable)
        // The UI's actual system state changes after an external disable; there
        // is no persisted login boolean to silently re-register the application.
        var observed = LoginItemState(.enabled)
        observed = LoginItemState(.notRegistered)
        XCTAssertFalse(observed == .enabled)
    }
}
