// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CodexEInk",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "EInkCore", targets: ["EInkCore"]),
        .executable(name: "eink-push", targets: ["EInkPush"]),
        .executable(name: "eink-core-tests", targets: ["EInkCoreTests"]),
        .executable(name: "eink-ble-probe", targets: ["EInkBLEProbe"]),
        .executable(name: "eink-companion", targets: ["EInkCompanion"]),
    ],
    targets: [
        .target(name: "EInkCore"),
        .executableTarget(name: "EInkPush", dependencies: ["EInkCore"]),
        .executableTarget(name: "EInkCoreTests", dependencies: ["EInkCore"], path: "Tests/EInkCoreTests"),
        .executableTarget(name: "EInkBLEProbe"),
        .executableTarget(name: "EInkCompanion", dependencies: ["EInkCore"]),
    ]
)
