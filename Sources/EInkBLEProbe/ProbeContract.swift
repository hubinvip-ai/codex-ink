import Foundation

enum ProbeOutcome: String { case pass, fail, notTested = "not-tested" }
enum ProbeAuthorization { case notDetermined, allowed, denied, restricted }
enum ProbePhase: String {
    case idle, authorization, ready, scanning, selection, connecting
    case services, characteristics, disconnecting, finished
}

struct ProbeCandidate {
    let identifier: UUID
    var name: String
    var rssi: Int
}

struct ProbeReport {
    static let stageNames = ["authorization", "manager_state", "discovered", "connected",
                             "service_matched", "characteristic_matched", "disconnected"]
    var stages = Dictionary(uniqueKeysWithValues: stageNames.map { ($0, ProbeOutcome.notTested) })
    var errorCode: String?
    var cleanupErrorCode: String?
    var selectedIdentifier: String?
    var selectedName: String?
    var managerState = "not-observed"
    var authorizationState = "not-observed"
    var characteristicProperties: [String] = []
    let bundleID: String
    let signingContext: String
    let startedAt = Date().timeIntervalSince1970

    func data(phase: ProbePhase) throws -> Data {
        #if arch(arm64)
        let architecture = "arm64"
        #elseif arch(x86_64)
        let architecture = "x86_64"
        #else
        let architecture = "unknown"
        #endif
        var object: [String: Any] = stages.mapValues(\.rawValue)
        object.merge([
            "version": 1, "phase": phase.rawValue,
            "error_code": errorCode as Any? ?? NSNull(),
            "cleanup_error_code": cleanupErrorCode as Any? ?? NSNull(),
            "selected_peripheral_identifier": selectedIdentifier as Any? ?? NSNull(),
            "selected_peripheral_name": selectedName as Any? ?? NSNull(),
            "manager_state_observed": managerState, "authorization_observed": authorizationState,
            "characteristic_properties": characteristicProperties,
            "expected_service_uuid": ProbeSession.serviceUUID,
            "expected_characteristic_uuid": ProbeSession.characteristicUUID,
            "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
            "architecture": architecture, "bundle_id": bundleID, "signing_context": signingContext,
            "started_at": startedAt, "updated_at": Date().timeIntervalSince1970,
            "screen_write_attempted": false,
            "not_validated": ["screen_refresh", "official_signing_and_notarization", "second_mac", "other_cpu_architectures", "sleep_restoration"],
        ]) { _, new in new }
        return try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
    }
}

/// Pure evidence/state model. No CoreBluetooth import and no radio side effects.
/// The UI adapter executes transitions; only observed callbacks pass stages.
final class ProbeSession {
    static let serviceUUID = "13187B10-EBA9-A3BA-044E-83D3217D9A38"
    static let characteristicUUID = "4B646063-6264-F3A7-8941-E65356EA82FE"
    private(set) var phase = ProbePhase.idle
    private(set) var report: ProbeReport
    private(set) var candidates: [UUID: ProbeCandidate] = [:]

    init(bundleID: String, signingContext: String) {
        report = ProbeReport(bundleID: bundleID, signingContext: signingContext)
    }

    var timeoutGroup: String? {
        switch phase {
        case .ready: return "ready"
        case .scanning: return "scan"
        case .connecting, .services, .characteristics: return "connection"
        case .disconnecting: return "disconnect"
        default: return nil
        }
    }
    var timeoutSeconds: TimeInterval? {
        guard let timeoutGroup else { return nil }
        return timeoutGroup == "disconnect" ? 5 : 30
    }
    var hasPendingConnection: Bool {
        [.connecting, .services, .characteristics, .disconnecting].contains(phase)
    }

    func begin() {
        guard phase == .idle else { return }
        phase = .authorization
    }

    func managerChanged(authorization: ProbeAuthorization, state: String) {
        guard phase != .idle && phase != .finished && phase != .disconnecting else { return }
        report.managerState = state
        switch authorization {
        case .notDetermined:
            report.authorizationState = "notDetermined"
            // Do not impose a readiness deadline on a system permission decision.
            if phase == .authorization || phase == .ready { phase = .authorization }
            return
        case .denied, .restricted:
            report.authorizationState = authorization == .denied ? "denied" : "restricted"
            report.stages["authorization"] = .fail
            fail(authorization == .denied ? "permission_denied" : "permission_restricted")
            return
        case .allowed:
            report.authorizationState = "allowedAlways"
            report.stages["authorization"] = .pass
        }
        if phase == .authorization { phase = .ready }
        if phase == .ready {
            if state == "poweredOn" {
                report.stages["manager_state"] = .pass
                phase = .scanning
            } else if state == "unsupported" || state == "unauthorized" {
                report.stages["manager_state"] = .fail
                fail(state == "unsupported" ? "bluetooth_unsupported" : "bluetooth_unauthorized")
            }
        } else if state != "poweredOn" {
            report.stages["manager_state"] = .fail
            failActiveStage()
            fail("bluetooth_unavailable")
        }
    }

    func discover(identifier: UUID, name: String, rssi: Int) {
        guard phase == .scanning else { return }
        candidates[identifier] = ProbeCandidate(identifier: identifier, name: name, rssi: rssi)
    }

    @discardableResult func select(identifier: UUID) -> Bool {
        guard phase == .scanning || phase == .selection, let candidate = candidates[identifier] else { return false }
        report.selectedIdentifier = identifier.uuidString
        report.selectedName = candidate.name
        report.stages["discovered"] = .pass
        phase = .connecting
        return true
    }

    func didConnect() {
        guard phase == .connecting else { return }
        report.stages["connected"] = .pass
        phase = .services
    }
    func didFailToConnect() {
        // Cancelling a pending connection can complete via didFailToConnect,
        // not didDisconnect. Never use this callback to close a proven connection.
        guard phase == .connecting || (phase == .disconnecting && report.stages["connected"] != .pass) else { return }
        report.stages["connected"] = .fail
        report.errorCode = report.errorCode ?? "connection_failed"
        phase = .finished
    }
    func didDiscoverServices(matched: Bool, hasError: Bool = false) {
        guard phase == .services else { return }
        report.stages["service_matched"] = matched && !hasError ? .pass : .fail
        if matched && !hasError { phase = .characteristics }
        else { fail(hasError ? "service_discovery_failed" : "service_mismatch") }
    }
    func didDiscoverCharacteristic(matched: Bool, properties: [String], hasError: Bool = false) {
        guard phase == .characteristics else { return }
        let writable = properties.contains("write")
        report.stages["characteristic_matched"] = matched && writable && !hasError ? .pass : .fail
        report.characteristicProperties = properties
        if !matched || hasError { report.errorCode = hasError ? "characteristic_discovery_failed" : "characteristic_mismatch" }
        else if !writable { report.errorCode = "characteristic_not_writable" }
        phase = .disconnecting
    }
    func didDisconnect(hasError: Bool) {
        guard hasPendingConnection else { return }
        // This callback confirms disconnection even when the peripheral dropped us.
        report.stages["disconnected"] = .pass
        if phase != .disconnecting {
            failActiveStage()
            report.errorCode = report.errorCode ?? "unexpected_disconnect"
        } else if hasError {
            report.errorCode = report.errorCode ?? "disconnect_error"
        }
        phase = .finished
    }

    func timedOut() {
        switch phase {
        case .ready:
            report.stages["manager_state"] = .fail
            fail("bluetooth_ready_timeout")
        case .scanning:
            if candidates.isEmpty {
                report.stages["discovered"] = .fail
                fail("scan_timeout")
            } else { phase = .selection }
        case .connecting, .services, .characteristics:
            failActiveStage()
            fail("connection_gatt_timeout")
        case .disconnecting:
            report.stages["disconnected"] = .fail
            report.cleanupErrorCode = "disconnect_timeout"
            report.errorCode = report.errorCode ?? "disconnect_timeout"
            phase = .finished
        default: break
        }
    }
    func cancel() {
        guard phase != .finished && phase != .disconnecting else { return }
        fail("user_cancelled")
    }
    func reportWriteFailed() {
        guard phase != .finished else { return }
        fail("report_write_failed")
    }
    private func fail(_ code: String) {
        report.errorCode = report.errorCode ?? code
        phase = hasPendingConnection ? .disconnecting : .finished
    }
    private func failActiveStage() {
        let stage: String?
        switch phase {
        case .connecting: stage = "connected"
        case .services: stage = "service_matched"
        case .characteristics: stage = "characteristic_matched"
        default: stage = nil
        }
        if let stage { report.stages[stage] = .fail }
    }
}

struct ProbeOptions {
    let reportURL: URL
    let deviceHint: String

    static func parse(_ arguments: [String]) throws -> ProbeOptions {
        var values: [String: String] = [:]
        var index = 0
        while index < arguments.count {
            let key = arguments[index]
            guard ["--report", "--device"].contains(key), values[key] == nil,
                  index + 1 < arguments.count, !arguments[index + 1].isEmpty,
                  !arguments[index + 1].hasPrefix("--") else { throw ProbeConfigurationError.invalidArguments }
            values[key] = arguments[index + 1]
            index += 2
        }
        guard let path = values["--report"], path.hasPrefix("/"), !path.hasSuffix("/") else {
            throw ProbeConfigurationError.invalidArguments
        }
        return ProbeOptions(reportURL: URL(fileURLWithPath: path), deviceHint: values["--device"] ?? "NRF_325608")
    }
}

enum ProbeConfigurationError: Error { case invalidArguments }

/// The first write reserves a NEW explicit report. Later checkpoints replace it
/// atomically; opening another probe cannot silently erase an earlier experiment.
final class ProbeReportStore {
    private let url: URL
    init(url: URL, initial: Data) throws {
        self.url = url
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try initial.write(to: url, options: [.withoutOverwriting])
    }
    func save(_ data: Data) throws { try data.write(to: url, options: [.atomic]) }
}
