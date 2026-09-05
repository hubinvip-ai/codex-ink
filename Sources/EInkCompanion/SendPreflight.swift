import Foundation

struct SendIdentity: Equatable {
    var sessionID: String
    var requestID: String
    var bindingRevision: Int
    var deviceIdentifier: UUID
    var maintenanceEpoch: UUID
}

/// One fresh, read-only inspection per request. No cached-ready fallback. All
/// post-await checks and the one send invocation execute on the same actor.
@MainActor
enum SendPreflight {
    enum Result {
        case allowed(SetupInspection, TimeInterval)
        case rejected(String, String, SetupInspection?)
        case superseded
    }
    static func authorize(
        identity: SendIdentity, installationID: String?, startedAt: TimeInterval,
        currentIdentity: () -> SendIdentity?, mayRun: () -> Bool, helperBusy: () -> Bool,
        inspect: () async throws -> SetupInspection,
        clock: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        send: (SetupInspection, TimeInterval) -> Void
    ) async -> Result {
        guard currentIdentity() == identity else { return .superseded }
        guard mayRun() else { return .rejected("send_failed", "发送条件已变化；当前帧保持待处理。", nil) }
        guard !helperBusy() else { return .rejected("send_failed", "配置检查正在进行，当前帧已延后；没有使用缓存授权。", nil) }
        do {
            let checked = try await inspect()
            guard currentIdentity() == identity else { return .superseded }
            guard mayRun(), !helperBusy() else { return .rejected("send_failed", "检查期间发送条件已变化；没有发送。", nil) }
            guard checked.isReady, installationID != nil, checked.installationID == installationID else {
                let reason = checked.blockers.isEmpty ? "安装或信任状态已变化，请重新检查配置。" : checked.blockers.map(SetupBlocker.explanation).joined(separator: "\n")
                let code = checked.isTemporarilyUnavailable ? "send_failed" : "device_not_configured"
                return .rejected(code, "发送前配置校验未通过：" + reason, checked)
            }
            // 130s bridge bound minus 5s disconnect cleanup and 1s pipe margin.
            // A slow helper consumes BLE's budget, never extends the request.
            let remaining = min(120, 124 - (clock() - startedAt))
            guard remaining.isFinite, remaining > 0 else { return .rejected("send_timeout", "配置检查后已无发送时间预算；没有发送。", checked) }
            send(checked, remaining)
            return .allowed(checked, remaining)
        } catch {
            guard currentIdentity() == identity else { return .superseded }
            let code: String
            if case CompanionError.unavailable = error { code = "send_failed" }
            else { code = "configuration_error" }
            return .rejected(code, "无法完成本帧配置检查；没有发送。" + error.localizedDescription, nil)
        }
    }
}
