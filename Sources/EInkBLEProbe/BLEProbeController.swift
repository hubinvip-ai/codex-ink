import AppKit
@preconcurrency import CoreBluetooth

/// The app itself owns CoreBluetooth. Constructing this controller does not
/// construct CBCentralManager; only the explicit UI Start action does that.
@MainActor
final class BLEProbeController: NSObject, @preconcurrency CBCentralManagerDelegate, @preconcurrency CBPeripheralDelegate {
    let session: ProbeSession
    let options: ProbeOptions
    var onChange: (() -> Void)?
    var onFinished: (() -> Void)?
    private(set) var exportFailed = false
    private let store: ProbeReportStore
    private var central: CBCentralManager?
    private var peripherals: [UUID: CBPeripheral] = [:]
    private var target: CBPeripheral?
    private var epdService: CBService?
    private var timer: Timer?
    private var timerGroup: String?

    init(options: ProbeOptions) throws {
        self.options = options
        session = ProbeSession(
            bundleID: Bundle.main.bundleIdentifier ?? "unbundled",
            signingContext: Bundle.main.object(forInfoDictionaryKey: "BLEProbeSigningContext") as? String ?? "unverified"
        )
        store = try ProbeReportStore(url: options.reportURL, initial: session.report.data(phase: .idle))
        super.init()
    }

    func start() {
        guard session.phase == .idle && !exportFailed else { return }
        update { session.begin() }
        guard session.phase == .authorization else { return }
        // This is the permission boundary. There is no manager before this click.
        // Avoid a second "turn Bluetooth on" alert; the UI reports readiness.
        central = CBCentralManager(delegate: self, queue: .main, options: [CBCentralManagerOptionShowPowerAlertKey: false])
        // Already-authorized launches get a readiness deadline even if the first
        // state callback is delayed. An undecided authorization still has none.
        if let central { centralManagerDidUpdateState(central) }
    }

    func select(_ identifier: UUID) {
        guard peripherals[identifier] != nil else { return }
        update { _ = session.select(identifier: identifier) }
    }

    func cancel() { update { session.cancel() } }

    private func update(_ action: () -> Void) {
        let previous = session.phase
        action()
        do { try store.save(session.report.data(phase: session.phase)) }
        catch {
            exportFailed = true
            session.reportWriteFailed()
        }
        let current = session.phase
        if previous == .scanning && current != .scanning { central?.stopScan() }
        syncTimer()
        if current != previous {
            switch current {
            case .scanning:
                // The screen does NOT advertise its EPD service UUID.
                central?.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
            case .connecting:
                if let selected = session.report.selectedIdentifier.flatMap(UUID.init(uuidString:)), let peripheral = peripherals[selected] {
                    target = peripheral
                    peripheral.delegate = self
                    central?.connect(peripheral, options: nil)
                }
            case .services:
                target?.discoverServices([CBUUID(string: ProbeSession.serviceUUID)])
            case .characteristics:
                if let epdService {
                    target?.discoverCharacteristics([CBUUID(string: ProbeSession.characteristicUUID)], for: epdService)
                }
            case .disconnecting:
                central?.stopScan()
                if let target { central?.cancelPeripheralConnection(target) }
            case .finished:
                central?.stopScan()
            default: break
            }
        }
        onChange?()
        if current == .finished && previous != .finished { onFinished?() }
    }

    private func syncTimer() {
        // Services/characteristics share the original connection deadline.
        guard timerGroup != session.timeoutGroup else { return }
        timer?.invalidate()
        timer = nil
        timerGroup = session.timeoutGroup
        guard let seconds = session.timeoutSeconds else { return }
        let next = Timer(timeInterval: seconds, target: self, selector: #selector(deadlineReached), userInfo: nil, repeats: false)
        timer = next
        RunLoop.main.add(next, forMode: .common)
    }

    @objc private func deadlineReached() { update { session.timedOut() } }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let authorization: ProbeAuthorization
        switch CBCentralManager.authorization {
        case .notDetermined: authorization = .notDetermined
        case .allowedAlways: authorization = .allowed
        case .denied: authorization = .denied
        case .restricted: authorization = .restricted
        @unknown default: authorization = .restricted
        }
        let state: String
        switch central.state {
        case .unknown: state = "unknown"
        case .resetting: state = "resetting"
        case .unsupported: state = "unsupported"
        case .unauthorized: state = "unauthorized"
        case .poweredOff: state = "poweredOff"
        case .poweredOn: state = "poweredOn"
        @unknown default: state = "unknown"
        }
        update { session.managerChanged(authorization: authorization, state: state) }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard session.phase == .scanning else { return }
        let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? peripheral.name ?? "未命名设备"
        peripherals[peripheral.identifier] = peripheral
        update { session.discover(identifier: peripheral.identifier, name: name, rssi: RSSI.intValue) }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard peripheral.identifier == target?.identifier else { return }
        // A late connection callback during cancellation must never start GATT.
        if session.phase == .disconnecting || session.phase == .finished {
            central.cancelPeripheralConnection(peripheral)
            return
        }
        update { session.didConnect() }
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        guard peripheral.identifier == target?.identifier else { return }
        update { session.didFailToConnect() }
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        guard peripheral.identifier == target?.identifier else { return }
        update { session.didDisconnect(hasError: error != nil) }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard peripheral.identifier == target?.identifier && session.phase == .services else { return }
        epdService = peripheral.services?.first { $0.uuid == CBUUID(string: ProbeSession.serviceUUID) }
        update { session.didDiscoverServices(matched: epdService != nil, hasError: error != nil) }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard peripheral.identifier == target?.identifier, session.phase == .characteristics,
              service.uuid == CBUUID(string: ProbeSession.serviceUUID) else { return }
        let characteristic = service.characteristics?.first { $0.uuid == CBUUID(string: ProbeSession.characteristicUUID) }
        var properties: [String] = []
        if let characteristic {
            for (property, label): (CBCharacteristicProperties, String) in [(.read, "read"), (.write, "write"), (.writeWithoutResponse, "writeWithoutResponse"), (.notify, "notify"), (.indicate, "indicate")] {
                if characteristic.properties.contains(property) { properties.append(label) }
            }
        }
        update { session.didDiscoverCharacteristic(matched: characteristic != nil, properties: properties, hasError: error != nil) }
        // Intentionally no reads, subscriptions, characteristic writes or firmware operations.
    }
}
