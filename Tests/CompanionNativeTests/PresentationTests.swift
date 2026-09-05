import Foundation

@MainActor
final class PresentationTests: NativeTestCase {
    func testSettingsNavigationUsesFourOperatorJobsWithOverviewFirst() {
        XCTAssertEqual(SettingsSection.allCases.map(\.rawValue), ["overview", "source", "display", "sync"])
        XCTAssertEqual(SettingsSection.allCases.map(\.title), ["概览", "数据源", "墨水屏", "自动同步"])
        XCTAssertEqual(SettingsSection.allCases.map(\.symbol), ["rectangle.grid.2x2", "terminal", "rectangle.inset.filled", "arrow.triangle.2.circlepath"])
        XCTAssertEqual(SettingsSection.default, .overview)
    }

    func testOnlyDamagedSettingsRequireTheSettingsRepairInstructions() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("eink-settings-guidance-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let arguments = ["--state-dir", directory.path, "--runtime-root", directory.path]

        // Construction reads configuration only; start(), helpers and Bluetooth
        // are never invoked. A missing configuration is a normal first launch.
        XCTAssertFalse(CompanionModel(arguments: arguments).settingsDamaged)
        let store = SettingsStore(directory: directory)
        try store.save(CompanionSettings(codexBinary: "/fixture/codex", pythonBinary: "/fixture/python"))
        let saved = CompanionModel(arguments: arguments)
        XCTAssertFalse(saved.settingsDamaged)
        XCTAssertEqual(saved.settings.pythonBinary, "/fixture/python")

        try Data("{broken settings".utf8).write(to: store.url)
        XCTAssertTrue(CompanionModel(arguments: arguments).settingsDamaged)
    }

    func testFreshDefaultPrefersSystemPythonWithoutOverridingExplicitSelection() {
        XCTAssertEqual(CompanionSettings.defaultPythonBinary(isExecutable: { _ in true }), "/usr/bin/python3")
        XCTAssertEqual(CompanionSettings.defaultPythonBinary(isExecutable: { $0 == "/opt/homebrew/bin/python3" }), "/opt/homebrew/bin/python3")
        XCTAssertEqual(CompanionSettings.defaultPythonBinary(bundleOverride: "/explicit/python", isExecutable: { _ in true }), "/explicit/python")
    }
}
