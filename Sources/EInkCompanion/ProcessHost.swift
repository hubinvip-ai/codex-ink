import Foundation
import Darwin

/// Foundation read(upToCount:) can fill the requested buffer before returning on
/// this macOS runtime. A protocol peer awaiting our reply must receive short lines
/// immediately, so use exactly one blocking POSIX read (retrying only EINTR).
private enum PipeRead {
    static func next(_ handle: FileHandle, cancellable: Bool = false) throws -> Data? {
        var buffer = [UInt8](repeating: 0, count: 8192)
        while true {
            if cancellable {
                // Only the reader task owns/closes this descriptor. Poll bounds
                // cancellation latency without closing an FD under a blocked
                // read, which could race descriptor reuse in a new session.
                if Task.isCancelled { return nil }
                var descriptor = pollfd(fd: handle.fileDescriptor, events: Int16(POLLIN), revents: 0)
                let ready = Darwin.poll(&descriptor, 1, 100)
                if ready == 0 || (ready < 0 && errno == EINTR) { continue }
                if ready < 0 { throw CompanionError.unavailable("数据管道读取失败。") }
                if Task.isCancelled { return nil }
            }
            let count = Darwin.read(handle.fileDescriptor, &buffer, buffer.count)
            if count > 0 { return Data(buffer.prefix(count)) }
            if count == 0 { return nil }
            if errno != EINTR { throw CompanionError.unavailable("数据管道读取失败。") }
        }
    }
}

/// One child and one ordered reader. Anonymous pipes only; no socket or BLE CLI.
@MainActor
final class WorkerHost {
    var onMessage: ((WorkerMessage) -> Void)?
    var onFault: ((String) -> Void)?
    var onSessionClosed: (() -> Void)?
    var onExit: ((Int32) -> Void)?
    private(set) var sessionID: String?
    private var process: Process?
    private var input: FileHandle?
    private var reader: Task<Void, Never>?
    private var shutdown: Task<Void, Never>?
    private var drainDeadline: Task<Void, Never>?
    private var framer = JSONLFramer()
    private var eof = false
    private var exitStatus: Int32?
    private var faulted = false
    private var sessionClosed = false
    var isRunning: Bool { process != nil }

    func start(executable: String, arguments: [String], sessionID: String) throws {
        guard process == nil else { throw CompanionError.unavailable("旧数据进程尚未退出。") }
        guard FileManager.default.isExecutableFile(atPath: executable) else { throw CompanionError.unavailable("Python 可执行文件不可用。") }
        signal(SIGPIPE, SIG_IGN)
        let child = Process(), output = Pipe(), inputPipe = Pipe()
        let writeFD = inputPipe.fileHandleForWriting.fileDescriptor
        guard fcntl(writeFD, F_SETFL, fcntl(writeFD, F_GETFL) | O_NONBLOCK) == 0 else { throw CompanionError.unavailable("无法建立非阻塞控制管道。") }
        child.executableURL = URL(fileURLWithPath: executable)
        child.arguments = arguments
        child.standardInput = inputPipe
        child.standardOutput = output
        child.standardError = FileHandle.nullDevice // Never log raw worker/account payloads.
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child.environment = environment
        self.sessionID = sessionID
        framer = JSONLFramer(); eof = false; exitStatus = nil; faulted = false; sessionClosed = false
        child.terminationHandler = { [weak self] child in
            let status = child.terminationStatus
            Task { @MainActor in self?.terminated(sessionID: sessionID, status: status) }
        }
        do { try child.run() } catch {
            self.sessionID = nil
            try? inputPipe.fileHandleForWriting.close()
            try? output.fileHandleForReading.close()
            throw CompanionError.unavailable("无法启动数据进程；请检查 Python 和运行目录。")
        }
        process = child
        input = inputPipe.fileHandleForWriting
        try? inputPipe.fileHandleForReading.close()
        try? output.fileHandleForWriting.close()
        let handle = output.fileHandleForReading
        reader = Task.detached { [weak self] in
            do {
                while let data = try PipeRead.next(handle, cancellable: true) {
                    await self?.receive(data, sessionID: sessionID)
                }
            } catch { /* EOF/session cleanup is handled below. */ }
            // Finish physical pipe cleanup BEFORE making onExit available.
            try? handle.close()
            await self?.endOfFile(sessionID: sessionID)
        }
    }

    func control(_ type: String, retry: Bool = false) throws {
        guard let sessionID else { throw CompanionError.invalidProtocol }
        try write(BridgeOutput.control(type, sessionID: sessionID, retry: retry))
    }
    func sendResult(sessionID: String, requestID: String, receipt: SendReceipt) throws {
        guard sessionID == self.sessionID, !faulted else { throw CompanionError.invalidProtocol }
        try write(BridgeOutput.result(sessionID: sessionID, requestID: requestID, receipt: receipt))
    }
    private func write(_ data: Data) throws {
        guard let input, !sessionClosed, !eof, process?.isRunning == true else { throw CompanionError.unavailable("数据管道已关闭。") }
        // Control/receipt messages are under PIPE_BUF. A full pipe fails closed
        // instead of blocking the UI while the worker is hung or no longer reads.
        let written = data.withUnsafeBytes { Darwin.write(input.fileDescriptor, $0.baseAddress, $0.count) }
        if written != data.count {
            protocolFailure("数据管道写入失败。")
            throw CompanionError.unavailable("数据管道写入失败。")
        }
    }
    func stop() {
        guard let process else { return }
        // Closing stdin is also the idle worker's stop signal.
        if let sessionID, let input, let bytes = try? BridgeOutput.control("stop", sessionID: sessionID) {
            _ = bytes.withUnsafeBytes { Darwin.write(input.fileDescriptor, $0.baseAddress, $0.count) }
        }
        closeSession()
        scheduleDrainIfExited()
        guard shutdown == nil else { return }
        shutdown = Task { [weak self, weak process] in
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled, let process, process.isRunning else { return }
            process.terminate()
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled, process.isRunning, self?.process === process else { return }
            // Exact child PID, never a process-name sweep.
            kill(process.processIdentifier, SIGKILL)
        }
    }
    private func receive(_ bytes: Data, sessionID: String) {
        guard sessionID == self.sessionID, !faulted, !sessionClosed, process?.isRunning == true else { return }
        do {
            for line in try framer.append(bytes) {
                let message = try WorkerMessage.decode(line)
                guard message.sessionID == sessionID else { throw CompanionError.invalidProtocol }
                guard !sessionClosed, process?.isRunning == true else { return }
                onMessage?(message)
                if faulted || sessionClosed { break }
            }
        } catch { protocolFailure(error.localizedDescription) }
    }
    private func protocolFailure(_ message: String) {
        guard !faulted else { return }
        faulted = true
        onFault?(message)
        stop()
    }
    private func endOfFile(sessionID: String) {
        guard sessionID == self.sessionID else { return }
        eof = true
        let wasClosed = sessionClosed
        closeSession()
        if !wasClosed { do { try framer.finish() } catch { protocolFailure(error.localizedDescription) } }
        // EOF is a session failure immediately, even if a broken worker stays alive.
        if exitStatus == nil { stop() }
        finishIfExited()
    }
    private func terminated(sessionID: String, status: Int32) {
        guard sessionID == self.sessionID else { return }
        exitStatus = status
        // A descendant retaining stdout is not the worker. Revoke trust and
        // notify the BLE owner immediately, without waiting for descendant EOF.
        closeSession()
        scheduleDrainIfExited()
        finishIfExited()
    }
    private func closeSession() {
        guard !sessionClosed else { return }
        sessionClosed = true
        try? input?.close(); input = nil
        onSessionClosed?()
    }
    private func scheduleDrainIfExited() {
        guard exitStatus != nil, !eof, drainDeadline == nil, let sessionID else { return }
        drainDeadline = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled, let self, self.sessionID == sessionID, !self.eof else { return }
            // Cancel the reader, not arbitrary descendant processes. Its poll
            // ends within 100 ms and its owned stdout FD is then closed.
            self.reader?.cancel()
        }
    }
    private func finishIfExited() {
        guard eof, let status = exitStatus, process != nil else { return }
        shutdown?.cancel(); shutdown = nil
        drainDeadline?.cancel(); drainDeadline = nil
        try? input?.close(); input = nil
        reader = nil; process = nil; sessionID = nil
        onExit?(status)
    }
}

struct HelperResult: Sendable { let stdout: Data; let status: Int32 }

/// A separate bounded host for inspect/preview/explicit setup actions.
@MainActor
final class HelperRunner {
    private var process: Process?
    private var continuation: CheckedContinuation<HelperResult, any Error>?
    private var output = Data()
    private var failure: (any Error)?
    private var status: Int32?
    private var eof = false
    private var deadline: Task<Void, Never>?
    private var escalation: Task<Void, Never>?
    private var reader: Task<Void, Never>?
    private var readOnlyDrain: Task<Void, Never>?
    private var readOnly = false
    private var token = UUID()
    var isRunning: Bool { process != nil }

    func run(executable: String, arguments: [String], timeout: TimeInterval = 115, limit: Int = 1_048_576, readOnly: Bool = false) async throws -> HelperResult {
        guard process == nil else { throw CompanionError.unavailable("另一个本地检查尚未结束。") }
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            output = Data(); failure = nil; status = nil; eof = false
            self.readOnly = readOnly
            let id = UUID(); token = id
            let child = Process(), pipe = Pipe()
            child.executableURL = URL(fileURLWithPath: executable)
            child.arguments = arguments
            child.standardInput = FileHandle.nullDevice
            child.standardOutput = pipe
            child.standardError = FileHandle.nullDevice
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            child.environment = environment
            child.terminationHandler = { [weak self] process in
                let status = process.terminationStatus
                Task { @MainActor in
                    guard let self, self.token == id else { return }
                    self.status = status
                    self.scheduleReadOnlyDrain()
                    self.finish()
                }
            }
            do { try child.run() } catch {
                self.continuation = nil
                continuation.resume(throwing: CompanionError.unavailable("无法运行本地检查；请检查可执行路径和 runtime。"))
                return
            }
            process = child
            try? pipe.fileHandleForWriting.close()
            let handle = pipe.fileHandleForReading
            reader = Task.detached { [weak self] in
                do {
                    while let data = try PipeRead.next(handle, cancellable: readOnly) {
                        await self?.received(data, token: id, limit: limit)
                    }
                } catch { /* termination still determines result */ }
                try? handle.close()
                await self?.ended(token: id)
            }
            deadline = Task { [weak self] in
                try? await Task.sleep(for: .seconds(timeout))
                guard !Task.isCancelled, self?.token == id else { return }
                self?.cancel(reason: "本地检查超时，未启用同步。")
            }
        }
    }
    func cancel(reason: String = "本地检查已取消。") {
        guard let process, failure == nil else { return }
        failure = CompanionError.unavailable(reason)
        // Inspect is read-only: cancellation may safely discard its output.
        // Install/restore keep the existing transaction lifecycle (default false).
        if readOnly { reader?.cancel() }
        if process.isRunning { process.terminate() }
        escalation = Task { [weak process] in
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled, let process, process.isRunning else { return }
            kill(process.processIdentifier, SIGKILL)
        }
    }
    private func received(_ bytes: Data, token: UUID, limit: Int) {
        guard token == self.token, failure == nil else { return }
        guard output.count + bytes.count <= limit else { cancel(reason: "本地检查返回超过大小限制，结果已拒绝。"); return }
        output.append(bytes)
    }
    private func ended(token: UUID) {
        guard token == self.token else { return }
        eof = true; finish()
    }
    private func scheduleReadOnlyDrain() {
        guard readOnly, !eof, readOnlyDrain == nil else { return }
        let id = token
        readOnlyDrain = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled, let self, self.token == id, !self.eof else { return }
            self.failure = self.failure ?? CompanionError.unavailable("只读检查进程退出后输出管道未关闭；结果已拒绝。")
            self.reader?.cancel()
        }
    }
    private func finish() {
        guard eof, let status, let continuation else { return }
        self.continuation = nil
        process = nil
        deadline?.cancel(); deadline = nil
        escalation?.cancel(); escalation = nil
        readOnlyDrain?.cancel(); readOnlyDrain = nil
        reader = nil
        if let failure { continuation.resume(throwing: failure) }
        else { continuation.resume(returning: HelperResult(stdout: output, status: status)) }
    }
}
