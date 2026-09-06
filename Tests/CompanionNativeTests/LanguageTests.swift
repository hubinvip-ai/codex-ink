import Foundation

@MainActor final class LanguageTests: NativeTestCase {
    func testLegacySettingsAndLanguageRoundTrip() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = SettingsStore(directory: root)
        var settings = CompanionSettings(codexBinary: "/a", pythonBinary: "/b")
        try settings.bind(identifier: UUID(), name: "测试设备", gattValidated: true, quiescent: true)
        settings.syncEnabled = true; settings.paused = true
        try store.save(settings)
        var legacy = try JSONSerialization.jsonObject(with: Data(contentsOf: store.url)) as! [String: Any]
        legacy.removeValue(forKey: "language")
        try JSONSerialization.data(withJSONObject: legacy).write(to: store.url)
        var restored = try XCTUnwrap(store.load())
        XCTAssertEqual(restored.language, .chinese)
        restored.language = .english
        try store.save(restored)
        XCTAssertEqual(try store.load(), restored)
        XCTAssertTrue(restored.syncEnabled && restored.paused)
        XCTAssertEqual(restored.deviceIdentifier, settings.deviceIdentifier)
        let options = CompanionOptions(stateDirectory: root, runtimeRoot: root)
        XCTAssertEqual(Array(options.previewArguments(settings: restored).suffix(2)), ["--language", "en"])
        XCTAssertEqual(Array(options.bridgeArguments(settings: restored, sessionID: "s").suffix(2)), ["--language", "en"])
        for bad in ["fr", "", NSNull()] as [Any] {
            var invalid = legacy; invalid["language"] = bad
            let bytes = try JSONSerialization.data(withJSONObject: invalid)
            try bytes.write(to: store.url)
            XCTAssertThrowsError(try store.load())
            XCTAssertThrowsError(try store.save(restored))
            XCTAssertEqual(try Data(contentsOf: store.url), bytes)
        }
    }
    func testLanguageSwitchRestartsRendererAndPreservesPause() async throws {
        for (enabled, paused) in [(false, false), (true, true), (true, false)] {
            let home = FileManager.default.homeDirectoryForCurrentUser
            XCTAssertTrue(home.path.contains("eink-native-tests-"))
            let root = home.appendingPathComponent("language-" + UUID().uuidString)
            let runtime = root.appendingPathComponent("tools")
            let state = root.appendingPathComponent("state")
            try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
            let setup = #"import json; print(json.dumps({'ok':True,'stage':'ready','blockers':[],'legacy_present':False,'installation_id':'test'}))"#
            try setup.write(to: runtime.appendingPathComponent("companion_setup.py"), atomically: true, encoding: .utf8)
            let worker = """
            import sys,json
            session=sys.argv[sys.argv.index('--session-id')+1]
            print(json.dumps(dict(version=1,type='ready',session_id=session)),flush=True)
            for line in sys.stdin:
                if json.loads(line)['type']=='stop': break
            """
            try worker.write(to: runtime.appendingPathComponent("companion_bridge.py"), atomically: true, encoding: .utf8)
            let preview = """
            import sys,json,time
            from pathlib import Path
            from PIL import Image
            state=Path(sys.argv[sys.argv.index('--state-dir')+1])
            language=sys.argv[sys.argv.index('--language')+1]
            Image.new('RGB',(400,300),'white' if language=='en' else 'black').save(state/'preview.png')
            (state/'preview-language').write_text(language)
            print(json.dumps(dict(status='preview',data_read_at=time.time())))
            """
            try preview.write(to: runtime.appendingPathComponent("codex_eink_reliable.py"), atomically: true, encoding: .utf8)
            var settings = CompanionSettings(codexBinary: "/usr/bin/true", pythonBinary: ProcessInfo.processInfo.environment["COMPANION_TEST_PYTHON"]!)
            try settings.bind(identifier: UUID(), name: "测试设备", gattValidated: true, quiescent: true)
            settings.syncEnabled = enabled; settings.paused = paused
            try SettingsStore(directory: state).save(settings)
            let model = CompanionModel(arguments: ["--state-dir", state.path, "--runtime-root", root.path])
            await model.start()
            await model.setLanguage(.english)
            XCTAssertEqual(model.settings.language, .english)
            XCTAssertEqual(model.settings.syncEnabled, enabled)
            XCTAssertEqual(model.settings.paused, paused)
            XCTAssertEqual(model.settings.deviceIdentifier, settings.deviceIdentifier)
            XCTAssertNotNil(model.previewImage)
            XCTAssertNil(model.errorMessage)
            XCTAssertEqual(try String(contentsOf: state.appendingPathComponent("preview-language"), encoding: .utf8), "en")
            XCTAssertEqual(model.workerRunning, enabled && !paused)
            XCTAssertEqual(try SettingsStore(directory: state).load()?.language, .english)
            await model.setLanguage(.chinese)
            XCTAssertEqual(try String(contentsOf: state.appendingPathComponent("preview-language"), encoding: .utf8), "zh-CN")
            let clean = await model.shutdown()
            XCTAssertTrue(clean)
        }
    }
    func testTranslationPreservesUserContentAndSupportsErrors() {
        XCTAssertEqual(DisplayLanguage.english.text("概览"), "Overview")
        XCTAssertEqual(DisplayLanguage.english.text("我的项目 · 运行中"), "我的项目 · 运行中")
        XCTAssertEqual(DisplayLanguage.english.text("正在下发 12/129 包"), "Sending packet 12/129")
        XCTAssertEqual(DisplayLanguage.english.text("配置检查失败：旧发送者仍在运行，请等待本次发送结束。"), "Setup check failed: The previous sender is still running. Wait for it to finish.")
    }
}
