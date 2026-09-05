import Foundation

func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { print("FAIL \(message)"); exit(1) }
}

let id = UUID(uuidString: "11111111-1111-1111-1111-111111111111")!
let otherID = UUID(uuidString: "22222222-2222-2222-2222-222222222222")!
func session() -> ProbeSession {
    ProbeSession(bundleID: "com.ben.codex-eink.ble-probe.dev", signingContext: "ad-hoc-development")
}
func scanning() -> ProbeSession {
    let probe = session()
    probe.begin()
    probe.managerChanged(authorization: .allowed, state: "poweredOn")
    return probe
}
func connecting() -> ProbeSession {
    let probe = scanning()
    probe.discover(identifier: id, name: "NRF_325608", rssi: -42)
    _ = probe.select(identifier: id)
    return probe
}
func connected() -> ProbeSession {
    let probe = connecting()
    probe.didConnect()
    return probe
}

// Run each callback interleaving separately so a failing cancel case does not
// hide the timeout case. This harness never initializes CoreBluetooth.
if let scenario = CommandLine.arguments.dropFirst(2).first {
    let probe = scenario == "connected-cancel" ? connected() : connecting()
    if scenario == "timeout" { probe.timedOut() } else { probe.cancel() }
    check(probe.phase == .disconnecting, "\(scenario): cleanup must initially wait for a callback")
    probe.didFailToConnect()
    if scenario == "connected-cancel" {
        check(probe.phase == .disconnecting, "an already-connected session still needs a disconnect callback")
        check(probe.report.stages["connected"] == .pass, "late failure must not rewrite successful connection evidence")
        check(probe.report.stages["disconnected"] == .notTested, "connection failure is not proof of disconnection")
        probe.didDisconnect(hasError: false)
        check(probe.phase == .finished && probe.report.stages["disconnected"] == .pass, "actual disconnect callback finishes connected cleanup")
    } else {
        check(probe.phase == .finished, "\(scenario): didFailToConnect must finish never-connected cleanup")
        check(probe.report.stages["connected"] == .fail, "\(scenario): failed connection is recorded")
        check(probe.report.stages["disconnected"] == .notTested, "\(scenario): no connection means no disconnect evidence")
        check(probe.timeoutSeconds == nil && probe.timeoutGroup == nil, "\(scenario): cleanup deadline must be removed")
        check(!probe.hasPendingConnection, "\(scenario): app termination must no longer wait")
        probe.timedOut()
        probe.didFailToConnect()
        probe.didDisconnect(hasError: false)
        let object = try JSONSerialization.jsonObject(with: probe.report.data(phase: probe.phase)) as! [String: Any]
        check(object["error_code"] as? String == (scenario == "timeout" ? "connection_gatt_timeout" : "user_cancelled"), "\(scenario): preserve the original error")
        check(object["cleanup_error_code"] is NSNull, "\(scenario): no fabricated disconnect timeout after cleanup")
        check(object["disconnected"] as? String == "not-tested", "\(scenario): late events cannot fabricate disconnect evidence")
    }
    print("PASS cleanup interleaving \(scenario)")
    exit(0)
}

let fresh = session()
check(fresh.phase == .idle && fresh.timeoutSeconds == nil, "launch must not scan or set timers")
check(fresh.report.stages.values.allSatisfy { $0 == .notTested }, "initial evidence is untested")
let initialJSON = try JSONSerialization.jsonObject(with: fresh.report.data(phase: fresh.phase)) as! [String: Any]
check(initialJSON["error_code"] is NSNull, "absent error is explicit JSON null")
check(initialJSON["os_version"] != nil && initialJSON["architecture"] != nil, "report captures platform")
check(initialJSON["bundle_id"] as? String == "com.ben.codex-eink.ble-probe.dev", "report captures app identity")
check(initialJSON["signing_context"] as? String == "ad-hoc-development", "development signing must be labelled")

fresh.begin()
fresh.managerChanged(authorization: .notDetermined, state: "unknown")
check(fresh.phase == .authorization && fresh.timeoutSeconds == nil, "undecided permission never times out")
fresh.timedOut()
check(fresh.phase == .authorization, "timeout cannot turn permission wait into device failure")
fresh.managerChanged(authorization: .denied, state: "unauthorized")
check(fresh.report.stages["authorization"] == .fail, "denial is authorization failure")
check(fresh.report.stages["discovered"] == .notTested, "denial leaves discovery untested")
check(fresh.report.errorCode == "permission_denied", "denial has enumerated error")

let ready = session()
ready.begin()
ready.managerChanged(authorization: .allowed, state: "poweredOff")
check(ready.phase == .ready && ready.timeoutSeconds == 30, "readiness has 30 second budget")
ready.timedOut()
check(ready.report.stages["manager_state"] == .fail, "readiness timeout is a failed stage")
check(ready.report.stages["discovered"] == .notTested, "no scan means not-tested")

let empty = scanning()
check(empty.timeoutSeconds == 30, "scan timeout is 30 seconds")
empty.timedOut()
check(empty.report.stages["discovered"] == .fail, "empty scan fails discovery")
check(empty.report.stages["connected"] == .notTested, "empty scan never connects")

let choice = scanning()
choice.discover(identifier: id, name: "NRF_325608", rssi: -42)
choice.discover(identifier: otherID, name: "NRF_325608", rssi: -50)
check(choice.report.selectedIdentifier == nil && choice.phase == .scanning, "same-name discovery never auto-selects")
check(choice.candidates.count == 2, "same-name devices are distinguished by peripheral ID")
check(!choice.select(identifier: UUID()), "cannot connect an undiscovered identifier")
choice.timedOut()
check(choice.phase == .selection && choice.timeoutSeconds == nil, "scan stops after 30s; selection can wait")
check(choice.select(identifier: otherID), "explicitly select discovered target")
check(choice.report.selectedIdentifier == otherID.uuidString, "persist chosen peripheral ID")
check(choice.timeoutSeconds == 30, "connection and GATT share 30 second budget")
choice.didConnect()
check(choice.phase == .services && choice.timeoutGroup == "connection", "GATT does not restart connection budget")
choice.didDiscoverServices(matched: true)
choice.didDiscoverCharacteristic(matched: true, properties: ["read", "write", "notify"])
check(choice.phase == .disconnecting && choice.timeoutSeconds == 5, "active disconnect has 5 second budget")
check(choice.report.stages["disconnected"] == .notTested, "cancel request is not disconnect evidence")
choice.didDisconnect(hasError: false)
check(choice.report.stages.values.allSatisfy { $0 == .pass }, "happy path requires seven callbacks/evidence stages")

let noAck = connected()
noAck.didDiscoverServices(matched: true)
noAck.didDiscoverCharacteristic(matched: true, properties: ["write"])
noAck.timedOut()
check(noAck.report.stages["disconnected"] == .fail && noAck.report.errorCode == "disconnect_timeout", "missing disconnect callback fails")
noAck.didDisconnect(hasError: false)
check(noAck.report.stages["disconnected"] == .fail, "late callback cannot rewrite finalized evidence")

let missingService = connected()
missingService.didDiscoverServices(matched: false)
check(missingService.report.stages["service_matched"] == .fail, "wrong service fails matching")
check(missingService.report.stages["characteristic_matched"] == .notTested, "wrong service does not invent characteristic evidence")
check(missingService.phase == .disconnecting, "service failure still cancels connection")
missingService.didDisconnect(hasError: false)
check(missingService.report.errorCode == "service_mismatch", "cleanup preserves root failure")

let missingCharacteristic = connected()
missingCharacteristic.didDiscoverServices(matched: true)
missingCharacteristic.didDiscoverCharacteristic(matched: false, properties: [])
check(missingCharacteristic.report.stages["characteristic_matched"] == .fail, "wrong characteristic fails matching")

let readOnlyCharacteristic = connected()
readOnlyCharacteristic.didDiscoverServices(matched: true)
readOnlyCharacteristic.didDiscoverCharacteristic(matched: true, properties: ["read"])
check(readOnlyCharacteristic.report.stages["characteristic_matched"] == .fail, "matching UUID alone cannot pass an unusable EPD characteristic")
check(readOnlyCharacteristic.report.errorCode == "characteristic_not_writable" && readOnlyCharacteristic.phase == .disconnecting, "property mismatch disconnects without writing")

let lost = connected()
lost.didDisconnect(hasError: true)
check(lost.report.stages["disconnected"] == .pass, "unexpected disconnect callback still confirms disconnection")
check(lost.report.stages["service_matched"] == .fail && lost.report.errorCode == "unexpected_disconnect", "unexpected disconnect fails active stage")

let failed = connecting()
failed.didFailToConnect()
check(failed.report.stages["connected"] == .fail && failed.report.stages["disconnected"] == .notTested, "failed connection cannot claim disconnect")
let timed = connected()
timed.timedOut()
check(timed.report.stages["service_matched"] == .fail && timed.phase == .disconnecting, "GATT timeout cleans up")
let cancelled = connecting()
cancelled.cancel()
check(cancelled.phase == .disconnecting && cancelled.report.errorCode == "user_cancelled", "quit waits for outstanding connection cancellation")
let poweredOff = connected()
poweredOff.managerChanged(authorization: .allowed, state: "poweredOff")
check(poweredOff.phase == .disconnecting && poweredOff.report.errorCode == "bluetooth_unavailable", "lost readiness does not continue GATT")

let options = try ProbeOptions.parse(["--report", "/tmp/probe.json", "--device", "NRF_325608"])
check(options.reportURL.path == "/tmp/probe.json" && options.deviceHint == "NRF_325608", "Launch Services arguments are accepted")
for arguments in [[], ["--report"], ["--report", "relative.json"], ["--report", "/tmp/a", "--write"], ["--report", "/tmp/a", "--device", ""], ["--report", "/tmp/a", "--report", "/tmp/b"]] {
    var rejected = false
    do { _ = try ProbeOptions.parse(arguments) } catch { rejected = true }
    check(rejected, "unsafe or ambiguous options must be rejected: \(arguments)")
}

let destination = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("evidence.json")
let store = try ProbeReportStore(url: destination, initial: session().report.data(phase: .idle))
try store.save(choice.report.data(phase: choice.phase))
var rejectedExisting = false
do { _ = try ProbeReportStore(url: destination, initial: Data()) } catch { rejectedExisting = true }
check(rejectedExisting, "new launch must not overwrite earlier report")
let persisted = try JSONSerialization.jsonObject(with: Data(contentsOf: destination)) as! [String: Any]
check(persisted["disconnected"] as? String == "pass", "failed overwrite preserves prior evidence")
print("PASS all probe contracts")
