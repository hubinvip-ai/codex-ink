import Foundation
import CryptoKit
import zlib
import EInkCore

@MainActor
final class PNGTests: NativeTestCase {
    // Raw fixtures intentionally bypass CoreGraphics and its color management.
    func png(width: Int = 400, height: Int = 300, rgba: [UInt8] = [198, 40, 40, 255]) -> Data {
        func be(_ n: Int) -> [UInt8] { [UInt8((n >> 24) & 255), UInt8((n >> 16) & 255), UInt8((n >> 8) & 255), UInt8(n & 255)] }
        func chunk(_ type: String, _ bytes: [UInt8]) -> [UInt8] {
            let body = Array(type.utf8) + bytes
            let checksum = body.withUnsafeBufferPointer { crc32(0, $0.baseAddress, uInt(body.count)) }
            return be(bytes.count) + body + be(Int(checksum))
        }
        let raw = Array(repeating: [UInt8(0)] + Array(repeating: rgba, count: width).flatMap { $0 }, count: height).flatMap { $0 }
        var length = compressBound(uLong(raw.count))
        var compressed = [UInt8](repeating: 0, count: Int(length))
        _ = compress(&compressed, &length, raw, uLong(raw.count))
        return Data([137,80,78,71,13,10,26,10] + chunk("IHDR", be(width) + be(height) + [8,6,0,0,0]) + chunk("IDAT", Array(compressed.prefix(Int(length)))) + chunk("IEND", []))
    }

    func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

    func testAcceptsExactNativeRedAndRejectsGrayTransparencyWrongSizeAndHash() throws {
        let red = png()
        let frame = try StrictPNG.decode(red, sha256: hash(red))
        XCTAssertEqual(frame.pixels.first, .red)
        XCTAssertEqual(frame.bitplanes().thirdColor.first, 255)
        for invalid in [png(rgba: [128,128,128,255]), png(rgba: [255,0,0,255]), png(rgba: [0,0,0,0]), png(width: 399), png(height: 301)] {
            XCTAssertThrowsError(try StrictPNG.decode(invalid, sha256: hash(invalid)))
        }
        XCTAssertThrowsError(try StrictPNG.decode(red, sha256: String(repeating: "0", count: 64)))
    }

    func testRejectsCorruptContainerTrailingBytesAndWrongSignatureEvenWithMatchingSHA() {
        let valid = png()
        var corrupt = valid
        corrupt[29] ^= 1
        for invalid in [corrupt, valid + Data([0]), Data(valid.dropLast(4)), Data([1,2,3])] {
            XCTAssertThrowsError(try StrictPNG.decode(invalid, sha256: hash(invalid)))
        }
    }

    func testFrozenSampleDecodesWithoutQuantizationOrPixelChanges() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let bytes = try Data(contentsOf: root.appendingPathComponent("docs/ui-baseline/public-v1.2/native.png"))
        let frame = try StrictPNG.decode(bytes, sha256: hash(bytes))
        let existing = try EInkFrame.loadAndQuantize(url: root.appendingPathComponent("docs/ui-baseline/public-v1.2/native.png"))
        XCTAssertEqual(frame, existing)
        XCTAssertEqual(frame.pixels.count, 120_000)
        XCTAssertEqual(Set(frame.pixels.map(\.rawValue)), [0, 1, 2])
        let commands = try EPDProtocol.commands(planes: frame.bitplanes(), chunkSize: 240)
        XCTAssertEqual(commands.count, 129)
        XCTAssertEqual(commands.reduce(0) { $0 + $1.count }, 30_511)
    }
}
