import CoreGraphics
import Foundation
import ImageIO
import EInkCore

func runFrameTests() throws {
    try expectEqual(EInkFrame.quantize(red: 255, green: 255, blue: 255), .white, "white quantization")
    try expectEqual(EInkFrame.quantize(red: 84, green: 84, blue: 84), .black, "black quantization")
    try expectEqual(EInkFrame.quantize(red: 198, green: 40, blue: 40), .red, "third-color quantization")
    try expectEqual(EInkFrame.quantize(red: 244, green: 235, blue: 235), .white, "near-white quantization")

    var pixels = Array(repeating: EInkColor.white, count: 400 * 300)
    pixels[0] = .black
    pixels[2] = .red
    pixels[4] = .black
    pixels[6] = .red
    let planes = try EInkFrame(width: 400, height: 300, pixels: pixels).bitplanes()
    try expectEqual(planes.blackWhite.count, 15_000, "black-white plane length")
    try expectEqual(planes.thirdColor.count, 15_000, "third-color plane length")
    try expectEqual(planes.blackWhite[0], 0x55, "black-white bit order")
    try expectEqual(planes.thirdColor[0], 0x22, "third-color bit order")
    try expect(planes.blackWhite.dropFirst().allSatisfy { $0 == 0xFF }, "white pixels remain white")
    try expect(planes.thirdColor.dropFirst().allSatisfy { $0 == 0x00 }, "non-red pixels stay clear")

    do {
        _ = try EInkFrame(width: 300, height: 400, pixels: Array(repeating: .white, count: 120_000))
        throw TestFailure("invalid frame dimensions were accepted")
    } catch is EInkFrameError {
        // Expected.
    }

    let temporaryDirectory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(at: temporaryDirectory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
    let sourceURL = temporaryDirectory.appendingPathComponent("source.png")
    let outputURL = temporaryDirectory.appendingPathComponent("quantized.png")
    try writeFixtureImage(to: sourceURL)

    let loaded = try EInkFrame.loadAndQuantize(url: sourceURL)
    try expectEqual(loaded.pixels[0], .black, "PNG loader black pixel")
    try expectEqual(loaded.pixels[1], .red, "PNG loader third-color pixel")
    try expectEqual(loaded.pixels[2], .white, "PNG loader white pixel")
    try loaded.writePNG(to: outputURL)

    guard let outputSource = CGImageSourceCreateWithURL(outputURL as CFURL, nil),
          let outputImage = CGImageSourceCreateImageAtIndex(outputSource, 0, nil) else {
        throw TestFailure("quantized PNG could not be read")
    }
    try expectEqual(outputImage.width, 400, "quantized PNG width")
    try expectEqual(outputImage.height, 300, "quantized PNG height")
}

private func writeFixtureImage(to url: URL) throws {
    var rgba = Array(repeating: UInt8(255), count: 400 * 300 * 4)
    rgba[0] = 0; rgba[1] = 0; rgba[2] = 0; rgba[3] = 255
    rgba[4] = 198; rgba[5] = 40; rgba[6] = 40; rgba[7] = 255
    let data = Data(rgba) as CFData
    guard let provider = CGDataProvider(data: data),
          let image = CGImage(
            width: 400,
            height: 300,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: 400 * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
          ),
          let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
        throw TestFailure("fixture image could not be created")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { throw TestFailure("fixture image could not be written") }
}
