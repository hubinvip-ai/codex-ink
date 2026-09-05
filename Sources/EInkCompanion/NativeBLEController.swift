import Foundation
import Combine
@preconcurrency import CoreBluetooth
import EInkCore

struct BLECandidate: Identifiable, Equatable {
    let id: UUID
    let name: String
    let rssi: Int
}

/// CoreBluetooth lives in the signed app, on one serial executor (the main actor).
/// Merely opening settings does not construct a CBCentralManager or ask permission.
@MainActor
final class NativeBLEController: NSObject, ObservableObject, @preconcurrency CBCentralManagerDelegate, @preconcurrency CBPeripheralDelegate {
    enum Phase { case idle, starting, scanning, selecting, connecting, services, characteristics, notifying, writing, disconnecting, cleanupBlocked }
    static let serviceUUID = "13187B10-EBA9-A3BA-044E-83D3217D9A38"
    static let characteristicUUID = "4B646063-6264-F3A7-8941-E65356EA82FE"
    @Published private(set) var candidates: [BLECandidate] = []
    @Published private(set) var detail = "未请求蓝牙授权"
    @Published private(set) var phase: Phase = .idle
    var onChange: (() -> Void)?
    var onRecovery: (() -> Void)?
    private var central: CBCentralManager?
    private var peripherals: [UUID: CBPeripheral] = [:]
    private var target: CBPeripheral?
    private var desiredIdentifier: UUID?
    private var characteristic: CBCharacteristic?
    private var transfer = BLETransfer()
    private var sendCompletion: ((SendReceipt) -> Void)?
    private var bindCompletion: ((Result<BLECandidate, any Error>) -> Void)?
    private var validatedCandidate: BLECandidate?
    private var operationError: String?
    private var timer: Timer?
    private var lastUnavailable = false
    var isQuiescent: Bool { phase == .idle || phase == .selecting }
    var isSending: Bool { sendCompletion != nil || transfer.active }
    var canSelect: Bool { phase == .scanning || phase == .selecting }

    func scan() {
        guard isQuiescent else { return }
        clearOperation()
        candidates = []; peripherals = [:]
        phase = .starting; detail = "正在检查蓝牙授权…"
        setDeadline(120)
        startManager()
        changed()
    }
    func validate(identifier: UUID, completion: @escaping (Result<BLECandidate, any Error>) -> Void) {
        guard canSelect, let peripheral = peripherals[identifier] else {
            completion(.failure(CompanionError.unavailable("请重新扫描并选择设备。"))); return
        }
        central?.stopScan()
        bindCompletion = completion
        desiredIdentifier = identifier
        operationError = nil; validatedCandidate = nil
        connect(peripheral)
        setDeadline(120)
    }
    func send(frame: EInkFrame, identifier: UUID, timeout: TimeInterval = 120, completion: @escaping (SendReceipt) -> Void) {
        guard isQuiescent else { completion(SendReceipt(errorCode: "send_failed")); return }
        guard timeout.isFinite, timeout > 0 else { completion(SendReceipt(errorCode: "send_timeout")); return }
        clearOperation()
        do { try transfer.begin(commands: EPDProtocol.commands(planes: frame.bitplanes(), chunkSize: 240)) }
        catch { completion(SendReceipt(errorCode: "invalid_frame")); return }
        sendCompletion = completion
        desiredIdentifier = identifier
        phase = .starting; detail = "正在连接已绑定屏幕…"
        setDeadline(min(120, timeout))
        startManager()
        changed()
    }
    func cancel() {
        guard !isQuiescent else { central?.stopScan(); phase = .idle; changed(); return }
        fail("send_failed", explanation: "正在停止蓝牙操作…")
    }
    private func startManager() {
        if let central { centralManagerDidUpdateState(central) }
        else { central = CBCentralManager(delegate: self, queue: .main, options: [CBCentralManagerOptionShowPowerAlertKey: false]) }
    }
    private func clearOperation() {
        timer?.invalidate(); timer = nil
        target?.delegate = nil
        target = nil; characteristic = nil; desiredIdentifier = nil
        operationError = nil; validatedCandidate = nil
        sendCompletion = nil; bindCompletion = nil
        transfer = BLETransfer()
    }
    private func connect(_ peripheral: CBPeripheral) {
        central?.stopScan()
        target = peripheral
        peripheral.delegate = self
        phase = .connecting; detail = "正在验证屏幕服务…"
        central?.connect(peripheral, options: nil)
        changed()
    }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let wasUnavailable = lastUnavailable
        // Initial unknown/notDetermined -> poweredOn is not recovery permission.
        if central.state == .poweredOff || central.state == .unauthorized || CBCentralManager.authorization == .denied || CBCentralManager.authorization == .restricted {
            lastUnavailable = true
        } else if central.state == .poweredOn && CBCentralManager.authorization == .allowedAlways {
            lastUnavailable = false
            if wasUnavailable { onRecovery?() }
        }
        guard !isQuiescent, phase != .disconnecting, phase != .cleanupBlocked else { changed(); return }
        if CBCentralManager.authorization == .denied || CBCentralManager.authorization == .restricted || central.state == .unauthorized {
            fail("permission_denied", explanation: "蓝牙权限未允许。请在系统设置中允许此伴侣应用。")
            return
        }
        if central.state == .poweredOff {
            fail("bluetooth_off", explanation: "蓝牙已关闭。开启后可重试。")
            return
        }
        if central.state == .unsupported { fail("send_failed", explanation: "此 Mac 不支持所需蓝牙功能。"); return }
        guard central.state == .poweredOn, phase == .starting else { return }
        if let id = desiredIdentifier, let peripheral = central.retrievePeripherals(withIdentifiers: [id]).first {
            connect(peripheral)
        } else {
            phase = .scanning
            detail = desiredIdentifier == nil ? "正在扫描；请选择你的屏幕。" : "正在寻找已绑定屏幕…"
            // This hardware does not advertise its image service UUID.
            central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
            if desiredIdentifier == nil { setDeadline(15) }
            changed()
        }
    }
    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard phase == .scanning else { return }
        if let desiredIdentifier {
            if peripheral.identifier == desiredIdentifier { connect(peripheral) }
            return
        }
        let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? peripheral.name ?? "未命名设备"
        let candidate = BLECandidate(id: peripheral.identifier, name: name, rssi: RSSI.intValue)
        peripherals[peripheral.identifier] = peripheral
        candidates.removeAll { $0.id == peripheral.identifier }
        candidates.append(candidate)
        candidates.sort { $0.rssi > $1.rssi }
        changed()
    }
    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard peripheral === target else { return }
        guard phase == .connecting else {
            if phase == .disconnecting || phase == .cleanupBlocked { central.cancelPeripheralConnection(peripheral) }
            return
        }
        phase = .services
        peripheral.discoverServices([CBUUID(string: Self.serviceUUID)])
        changed()
    }
    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        guard peripheral === target, [.connecting, .disconnecting, .cleanupBlocked].contains(phase) else { return }
        operationError = operationError ?? "send_failed"
        transfer.cancel(code: operationError!)
        finishDisconnected(error: true)
    }
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        guard peripheral === target else { return }
        if phase != .disconnecting && phase != .cleanupBlocked { operationError = operationError ?? "send_failed" }
        finishDisconnected(error: error != nil)
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard peripheral === target, phase == .services else { return }
        guard error == nil, let service = peripheral.services?.first(where: { $0.uuid == CBUUID(string: Self.serviceUUID) }) else {
            fail("send_failed", explanation: "设备没有匹配的 EPD 服务，未保存绑定。"); return
        }
        phase = .characteristics
        peripheral.discoverCharacteristics([CBUUID(string: Self.characteristicUUID)], for: service)
        changed()
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard peripheral === target, phase == .characteristics, service.uuid == CBUUID(string: Self.serviceUUID) else { return }
        guard error == nil, let characteristic = service.characteristics?.first(where: { $0.uuid == CBUUID(string: Self.characteristicUUID) }),
              characteristic.properties.contains(.write), peripheral.maximumWriteValueLength(for: .withResponse) >= 244 else {
            fail("send_failed", explanation: "设备写入特征或包长不兼容，未保存绑定。"); return
        }
        self.characteristic = characteristic
        if bindCompletion != nil {
            validatedCandidate = candidates.first { $0.id == peripheral.identifier }
            disconnect()
        } else if characteristic.properties.contains(.notify) {
            phase = .notifying
            peripheral.setNotifyValue(true, for: characteristic)
        } else { phase = .writing; writeNext() }
        changed()
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard peripheral === target, characteristic === self.characteristic, phase == .notifying else { return }
        guard error == nil, characteristic.isNotifying else { fail("send_failed", explanation: "设备通知初始化失败。"); return }
        phase = .writing; writeNext()
    }
    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard peripheral === target, characteristic === self.characteristic, phase == .writing else { return }
        transfer.wrote(error: error != nil)
        if error != nil { fail("send_failed", explanation: "数据包写入失败；未确认此帧。") }
        else if transfer.needsDisconnect { disconnect() }
        else { writeNext() }
    }
    private func writeNext() {
        guard phase == .writing, let packet = transfer.nextPacket, let target, let characteristic else { return }
        detail = "正在下发 \(transfer.receipt.packets + 1)/129 包"
        target.writeValue(packet, for: characteristic, type: .withResponse)
        changed()
    }
    private func fail(_ code: String, explanation: String) {
        guard phase != .cleanupBlocked else { return }
        operationError = operationError ?? code
        transfer.cancel(code: code)
        detail = explanation
        central?.stopScan()
        if target != nil { disconnect() }
        else { finishDisconnected(error: true) }
    }
    private func disconnect() {
        guard phase != .disconnecting && phase != .cleanupBlocked else { return }
        central?.stopScan()
        phase = .disconnecting
        if let target { central?.cancelPeripheralConnection(target) }
        setDeadline(5)
        changed()
    }
    private func finishDisconnected(error: Bool) {
        timer?.invalidate(); timer = nil
        if let operationError { transfer.cancel(code: operationError) }
        transfer.disconnected(error: error)
        let send = sendCompletion, bind = bindCompletion
        sendCompletion = nil; bindCompletion = nil
        let receipt = transfer.receipt
        let candidate = validatedCandidate
        let validBinding = operationError == nil && !error && candidate != nil
        target?.delegate = nil; target = nil; characteristic = nil
        phase = .idle
        if receipt.ok { detail = "129/129 包已写入并确认断开；实屏外观仍需确认。" }
        else if validBinding { detail = "服务验证通过，已断开。" }
        else if operationError == nil { detail = "设备提前断开；未确认发送。" }
        send?(receipt)
        if let bind {
            if validBinding, let candidate { bind(.success(candidate)) }
            else { bind(.failure(CompanionError.unavailable(detail))) }
        }
        changed()
    }
    private func setDeadline(_ seconds: TimeInterval) {
        timer?.invalidate()
        timer = Timer(timeInterval: seconds, target: self, selector: #selector(timedOut), userInfo: nil, repeats: false)
        RunLoop.main.add(timer!, forMode: .common)
    }
    @objc private func timedOut() {
        timer?.invalidate(); timer = nil
        if phase == .scanning && desiredIdentifier == nil {
            central?.stopScan(); phase = .selecting
            detail = candidates.isEmpty ? "未发现设备。确认屏幕可发现后重新扫描。" : "扫描已结束；请选择要绑定的屏幕。"
            changed(); return
        }
        if phase == .disconnecting {
            // Do NOT start a new worker/connection after an unconfirmed disconnect.
            phase = .cleanupBlocked
            operationError = operationError ?? "send_timeout"
            transfer.cleanupTimedOut()
            detail = "断开未获确认，已阻止重连。等待系统断开回调，或退出后重试。"
            let send = sendCompletion, bind = bindCompletion
            sendCompletion = nil; bindCompletion = nil
            send?(transfer.receipt)
            bind?(.failure(CompanionError.unavailable(detail)))
            changed(); return
        }
        fail("send_timeout", explanation: "蓝牙操作超过本帧时间预算，正在清理连接。")
    }
    private func changed() { onChange?() }
}
