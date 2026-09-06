import Foundation
import EInkCore

@MainActor
final class HostTests: NativeTestCase {
    func testDirectTerminationInvalidatesSessionAndBoundsDescendantPipeDrain() async throws {
        let host = WorkerHost()
        var closes = 0, exits = 0, messages = 0
        var exitStatus: Int32?
        var transfer = BLETransfer()
        let frame = try EInkFrame(width: 400, height: 300, pixels: Array(repeating: .white, count: 120_000))
        try transfer.begin(commands: EPDProtocol.commands(planes: frame.bitplanes(), chunkSize: 240))
        host.onSessionClosed = { closes += 1; transfer.cancel(code: "send_failed") }
        host.onMessage = { _ in messages += 1 }
        host.onExit = { status in
            exits += 1; exitStatus = status
            var policy = RestartPolicy()
            XCTAssertNil(policy.delay(processExited: !host.isRunning, bleQuiescent: transfer.quiescent, allowed: true))
        }
        let script = #"""
import os,time,json
if os.fork()==0:
 time.sleep(.35)
 print(json.dumps(dict(version=1,type='ready',session_id='held')),flush=True)
 time.sleep(3)
 os._exit(0)
os._exit(7)
"""#
        try host.start(executable: "/usr/bin/python3", arguments: ["-B", "-c", script], sessionID: "held")
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(closes, 1)
        XCTAssertEqual(messages, 0) // A descendant cannot impersonate its dead parent.
        XCTAssertFalse(transfer.quiescent)
        XCTAssertThrowsError(try host.control("resume"))
        for _ in 0..<80 where exits == 0 { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(exits, 1)
        XCTAssertEqual(exitStatus, 7)
        XCTAssertFalse(host.isRunning)
        XCTAssertEqual(closes, 1)
        transfer.disconnected(error: false)
        var policy = RestartPolicy()
        XCTAssertEqual(policy.delay(processExited: !host.isRunning, bleQuiescent: transfer.quiescent, allowed: true), 5)
    }

    func testStopAfterDirectExitDoesNotWaitForDescendantEOFOrDuplicateCallbacks() async throws {
        let host = WorkerHost()
        var closes = 0, exits = 0
        host.onSessionClosed = { closes += 1 }
        host.onExit = { _ in exits += 1 }
        let script = "import os,time\nif os.fork()==0:\n time.sleep(4)\n os._exit(0)\nos._exit(0)"
        try host.start(executable: "/usr/bin/python3", arguments: ["-B", "-c", script], sessionID: "stop-held")
        try await Task.sleep(for: .milliseconds(300))
        host.stop()
        host.stop()
        XCTAssertEqual(closes, 1)
        for _ in 0..<80 where exits == 0 { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(exits, 1)
        XCTAssertFalse(host.isRunning)
        XCTAssertEqual(closes, 1)
    }

    func testUnresponsiveChildCannotBlockMainActorWritingControls() async throws {
        let host = WorkerHost()
        var fault = false
        var exited = false
        host.onFault = { _ in fault = true }
        host.onExit = { _ in exited = true }
        try host.start(executable: "/bin/sleep", arguments: ["2"], sessionID: "fixture")
        let began = Date()
        for _ in 0..<10_000 {
            do { try host.control("refresh") } catch { break }
        }
        XCTAssertTrue(Date().timeIntervalSince(began) < 1)
        XCTAssertTrue(fault)
        for _ in 0..<200 where !exited { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertTrue(exited)
    }

    func testEOFInvalidatesSessionBeforeChildTerminationSoBLECanCancelImmediately() async throws {
        let host = WorkerHost()
        var closed = false
        var exited = false
        host.onSessionClosed = { closed = true }
        host.onExit = { _ in exited = true }
        try host.start(executable: "/bin/sh", arguments: ["-c", "exec 1>&-; sleep 1"], sessionID: "fixture")
        for _ in 0..<30 where !closed { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(closed)
        XCTAssertFalse(exited)
        for _ in 0..<200 where !exited { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertTrue(exited)
    }

    func testRealChildMalformedOrWrongSessionOutputClosesProtocol() async throws {
        for content in ["not json", #"{"version":1,"type":"ready","session_id":"wrong"}"#] {
            let host = WorkerHost()
            var failed = false
            var exited = false
            var accepted = 0
            host.onFault = { _ in failed = true }
            host.onMessage = { _ in accepted += 1 }
            host.onExit = { _ in exited = true }
            // Remain alive until the host rejects the line. Messages queued
            // after direct termination intentionally no longer have authority.
            try host.start(executable: "/bin/sh", arguments: ["-c", "printf '%s\\n' \"$1\"; read reply", "fixture", content], sessionID: "fixture")
            for _ in 0..<100 where !exited { try await Task.sleep(for: .milliseconds(20)) }
            XCTAssertTrue(failed)
            XCTAssertTrue(exited)
            XCTAssertEqual(accepted, 0)
        }
    }
    func testChildFragmentedReadyThenEOFClosesSessionWithoutUIOrBluetooth() async throws {
        let host = WorkerHost()
        var messages: [WorkerMessage] = []
        var exited = false
        host.onMessage = { messages.append($0); host.stop() }
        host.onExit = { _ in exited = true }
        try host.start(executable: "/bin/sh", arguments: ["-c", "printf '%s' '{\"version\":1,\"type\":\"ready\",'; printf '%s\\n' '\"session_id\":\"fixture\"}'; read reply"], sessionID: "fixture")
        for _ in 0..<100 where !exited { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertTrue(exited)
        XCTAssertEqual(messages.count, 1)
        XCTAssertFalse(host.isRunning)
    }

    func testStopClosesIdleChildStdinAndAwaitsTermination() async throws {
        let host = WorkerHost()
        var exited = false
        host.onExit = { _ in exited = true }
        try host.start(executable: "/usr/bin/tee", arguments: [], sessionID: "fixture")
        host.stop()
        for _ in 0..<100 where !exited { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertTrue(exited)
        XCTAssertFalse(host.isRunning)
    }

    func testWorkerPipeCarriesExplicitRetryAndExactReceiptToChild() async throws {
        let host = WorkerHost()
        let script = #"""
import sys,json
def emit(t, **kw):
 print(json.dumps(dict(version=1,type=t,session_id='fixture',**kw)),flush=True)
emit('ready')
r=json.loads(sys.stdin.readline())
assert r==dict(version=1,type='resume',session_id='fixture',retry=True)
emit('send',request_id='request',png_base64='eA==',png_sha256='0'*64)
r=json.loads(sys.stdin.readline())
assert r==dict(version=1,type='send_result',session_id='fixture',request_id='request',ok=True,packets=129,bytes=30511,disconnected=True)
emit('status',payload={'status':'fixture_verified'})
sys.stdin.readline()
"""#
        var verified = false
        var exitStatus: Int32?
        host.onMessage = { message in
            switch message {
            case .ready: try? host.control("resume", retry: true)
            case .send(let s, let r, _, _): try? host.sendResult(sessionID: s, requestID: r, receipt: SendReceipt(packets: 129, bytes: 30_511, disconnected: true))
            case .status(_, let payload): verified = payload["status"]?.string == "fixture_verified"; host.stop()
            }
        }
        host.onExit = { exitStatus = $0 }
        try host.start(executable: "/usr/bin/python3", arguments: ["-B", "-c", script], sessionID: "fixture")
        for _ in 0..<200 where exitStatus == nil { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(exitStatus, 0)
        XCTAssertTrue(verified)
    }

    func testHelperArgumentsAreNamedAndPreviewRequiresValidatedSource() throws {
        let options = try CompanionOptions.parse(["--state-dir", "/tmp/isolated state", "--runtime-root", "/tmp/runtime root"])
        let settings = CompanionSettings(codexBinary: "/Applications/Codex.app/Contents/Resources/codex", pythonBinary: "/tmp/python")
        XCTAssertEqual(options.previewArguments(settings: settings), ["/tmp/runtime root/tools/codex_eink_reliable.py", "--state-dir", "/tmp/isolated state", "--state-file", "/tmp/isolated state/status.json", "preview", "--codex-binary", settings.codexBinary, "--validated", "--language", "zh-CN"])
        XCTAssertEqual(options.setupArguments(action: "install", settings: settings), ["/tmp/runtime root/tools/companion_setup.py", "--state-dir", "/tmp/isolated state", "install", "--python", "/tmp/python", "--runtime-root", "/tmp/runtime root"])
        XCTAssertThrowsError(try CompanionOptions.parse(["--state-dir", "relative"]))
    }

    func testOneShotHelperBoundsOutputAndReportsNonzeroWithoutLeakingStderr() async throws {
        let helper = HelperRunner()
        let result = try await helper.run(executable: "/bin/sh", arguments: ["-c", "printf '{}'; printf 'private text' >&2; exit 3"], timeout: 2)
        XCTAssertEqual(result.status, 3)
        XCTAssertEqual(result.stdout, Data("{}".utf8))
        do {
            _ = try await helper.run(executable: "/usr/bin/yes", arguments: ["x"], timeout: 2, limit: 128)
            XCTFail("Oversized child output must fail")
        } catch { XCTAssertFalse(error.localizedDescription.contains("private text")) }
    }
}
