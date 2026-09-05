@preconcurrency import CoreBluetooth
import EInkCore
import Foundation

private struct Options {
    let inputURL: URL
    let quantizedOutputURL: URL?
    let deviceName: String
    let prepareOnly: Bool

    static func parse(_ arguments: [String]) throws -> Options {
        var input: String?
        var output: String?
        var deviceName = "NRF_325608"
        var prepareOnly = false
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--input":
                index += 1
                guard index < arguments.count else { throw CLIError.missingValue("--input") }
                input = arguments[index]
            case "--quantized-output":
                index += 1
                guard index < arguments.count else { throw CLIError.missingValue("--quantized-output") }
                output = arguments[index]
            case "--device":
                index += 1
                guard index < arguments.count else { throw CLIError.missingValue("--device") }
                deviceName = arguments[index]
            case "--prepare-only": prepareOnly = true
            case "--help", "-h": printUsage(); exit(0)
            default: throw CLIError.unknownArgument(arguments[index])
            }
            index += 1
        }
        guard let input else { throw CLIError.missingArgument("--input") }
        return Options(
            inputURL: URL(fileURLWithPath: input),
            quantizedOutputURL: output.map { URL(fileURLWithPath: $0) },
            deviceName: deviceName,
            prepareOnly: prepareOnly
        )
    }
}

private enum CLIError: Error, CustomStringConvertible {
    case missingArgument(String)
    case missingValue(String)
    case unknownArgument(String)
    var description: String {
        switch self {
        case .missingArgument(let value): return "缺少参数 \(value)"
        case .missingValue(let value): return "参数 \(value) 缺少值"
        case .unknownArgument(let value): return "未知参数 \(value)"
        }
    }
}

private func printUsage() {
    print("用法: eink-push --input <400x300 image> [--quantized-output <png>] [--device NRF_325608] [--prepare-only]")
}

private final class BLEImagePusher: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    private static let epdService = CBUUID(string: "13187B10-EBA9-A3BA-044E-83D3217D9A38")
    private static let epdCharacteristic = CBUUID(string: "4B646063-6264-F3A7-8941-E65356EA82FE")
    private let deviceName: String
    private let commands: [Data]
    private var central: CBCentralManager!
    private var target: CBPeripheral?
    private var writableCharacteristic: CBCharacteristic?
    private var commandIndex = 0
    private var timeout: Timer?
    private var completed = false

    init(deviceName: String, commands: [Data]) {
        self.deviceName = deviceName
        self.commands = commands
        super.init()
    }

    func start() {
        timeout = Timer.scheduledTimer(timeInterval: 120, target: self, selector: #selector(timeoutFired), userInfo: nil, repeats: false)
        central = CBCentralManager(delegate: self, queue: .main)
    }

    @objc private func timeoutFired() {
        finish(code: 2, message: "BLE 操作超时")
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard central.state == .poweredOn else {
            if central.state != .unknown, central.state != .resetting {
                finish(code: 2, message: "蓝牙不可用，状态值 \(central.state.rawValue)")
            }
            return
        }
        print("SCAN device=\(deviceName)")
        // The device exposes the EPD service after connection but does not advertise it.
        central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let localName = advertisementData[CBAdvertisementDataLocalNameKey] as? String
        guard peripheral.name == deviceName || localName == deviceName else { return }
        print("FOUND name=\(deviceName) rssi=\(RSSI)")
        self.central.stopScan()
        target = peripheral
        peripheral.delegate = self
        self.central.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("CONNECTED maxWrite=\(peripheral.maximumWriteValueLength(for: .withResponse))")
        peripheral.discoverServices([Self.epdService])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        finish(code: 2, message: "连接失败: \(error?.localizedDescription ?? "unknown")")
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        if !completed { finish(code: 2, message: "设备提前断开: \(error?.localizedDescription ?? "unknown")") }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error { return finish(code: 2, message: "服务发现失败: \(error.localizedDescription)") }
        guard let service = peripheral.services?.first(where: { $0.uuid == Self.epdService }) else {
            return finish(code: 2, message: "未找到 EPD Service")
        }
        peripheral.discoverCharacteristics([Self.epdCharacteristic], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error { return finish(code: 2, message: "特征发现失败: \(error.localizedDescription)") }
        guard let characteristic = service.characteristics?.first(where: { $0.uuid == Self.epdCharacteristic }), characteristic.properties.contains(.write) else {
            return finish(code: 2, message: "未找到可写 EPD Characteristic")
        }
        let maxPacket = commands.map(\.count).max() ?? 0
        guard peripheral.maximumWriteValueLength(for: .withResponse) >= maxPacket else {
            return finish(code: 2, message: "BLE MTU 不足: 需要 \(maxPacket) bytes")
        }
        writableCharacteristic = characteristic
        if characteristic.properties.contains(.notify) { peripheral.setNotifyValue(true, for: characteristic) } else { writeNext() }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error { return finish(code: 2, message: "通知启用失败: \(error.localizedDescription)") }
        writeNext()
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error { return finish(code: 2, message: "第 \(commandIndex + 1) 包写入失败: \(error.localizedDescription)") }
        commandIndex += 1
        writeNext()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, let value = characteristic.value, !value.isEmpty else { return }
        print("NOTIFY \(value.map { String(format: "%02X", $0) }.joined())")
    }

    private func writeNext() {
        guard let target, let writableCharacteristic else { return }
        guard commandIndex < commands.count else {
            completed = true
            print("WRITE_COMPLETE packets=\(commands.count) bytes=\(commands.reduce(0) { $0 + $1.count })")
            central.cancelPeripheralConnection(target)
            finish(code: 0, message: "刷新命令已发送，设备已断开")
            return
        }
        if commandIndex == 0 || commandIndex == 2 || commandIndex == 36 || commandIndex == commands.count - 1 || commandIndex % 10 == 0 {
            print("WRITE packet=\(commandIndex + 1)/\(commands.count) size=\(commands[commandIndex].count)")
        }
        target.writeValue(commands[commandIndex], for: writableCharacteristic, type: .withResponse)
    }

    private func finish(code: Int32, message: String) {
        guard timeout != nil else { return }
        timeout?.invalidate()
        timeout = nil
        if code == 0 { print("SUCCESS \(message)") } else { fputs("ERROR \(message)\n", stderr) }
        exit(code)
    }
}

do {
    let options = try Options.parse(Array(CommandLine.arguments.dropFirst()))
    let frame = try EInkFrame.loadAndQuantize(url: options.inputURL)
    if let output = options.quantizedOutputURL {
        try FileManager.default.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
        try frame.writePNG(to: output)
        print("QUANTIZED path=\(output.path)")
    }
    // Keep each packet within the negotiated 244-byte ATT payload. Larger
    // CoreBluetooth long writes cause this DA14585 firmware to disconnect.
    let commands = try EPDProtocol.commands(planes: frame.bitplanes(), chunkSize: 240)
    print("FRAME width=400 height=300 colors=3 packets=\(commands.count)")
    if options.prepareOnly { exit(0) }
    let pusher = BLEImagePusher(deviceName: options.deviceName, commands: commands)
    pusher.start()
    RunLoop.main.run()
} catch {
    fputs("ERROR \(error)\n", stderr)
    printUsage()
    exit(64)
}
