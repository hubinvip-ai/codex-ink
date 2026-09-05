import CoreGraphics
import Foundation
import ImageIO

public enum EInkColor: UInt8, Equatable, Sendable {
    case white
    case black
    case red
}

public enum EInkFrameError: Error, Equatable {
    case invalidDimensions(width: Int, height: Int)
    case invalidPixelCount(expected: Int, actual: Int)
    case imageLoadFailed(String)
    case imageWriteFailed(String)
}

public struct EInkBitplanes: Equatable, Sendable {
    public let blackWhite: [UInt8]
    public let thirdColor: [UInt8]

    public init(blackWhite: [UInt8], thirdColor: [UInt8]) {
        self.blackWhite = blackWhite
        self.thirdColor = thirdColor
    }
}

public struct EInkFrame: Equatable, Sendable {
    public static let width = 400
    public static let height = 300
    public let pixels: [EInkColor]

    public init(width: Int, height: Int, pixels: [EInkColor]) throws {
        guard width == Self.width, height == Self.height else {
            throw EInkFrameError.invalidDimensions(width: width, height: height)
        }
        let expected = Self.width * Self.height
        guard pixels.count == expected else {
            throw EInkFrameError.invalidPixelCount(expected: expected, actual: pixels.count)
        }
        self.pixels = pixels
    }

    public static func quantize(red: UInt8, green: UInt8, blue: UInt8) -> EInkColor {
        let redValue = Int(red)
        let greenValue = Int(green)
        let blueValue = Int(blue)
        if redValue >= 120, redValue - greenValue >= 60, redValue - blueValue >= 60 {
            return .red
        }
        let luminance = (299 * redValue + 587 * greenValue + 114 * blueValue) / 1_000
        return luminance < 128 ? .black : .white
    }

    public static func loadAndQuantize(url: URL) throws -> EInkFrame {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw EInkFrameError.imageLoadFailed(url.path)
        }
        guard image.width == width, image.height == height else {
            throw EInkFrameError.invalidDimensions(width: image.width, height: image.height)
        }

        var rgba = Array(repeating: UInt8(0), count: width * height * 4)
        let rendered = rgba.withUnsafeMutableBytes { bytes -> Bool in
            guard let baseAddress = bytes.baseAddress,
                  let context = CGContext(
                    data: baseAddress,
                    width: width,
                    height: height,
                    bitsPerComponent: 8,
                    bytesPerRow: width * 4,
                    space: CGColorSpaceCreateDeviceRGB(),
                    bitmapInfo: CGBitmapInfo.byteOrder32Big.rawValue | CGImageAlphaInfo.premultipliedLast.rawValue
                  ) else { return false }
            context.interpolationQuality = .none
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard rendered else { throw EInkFrameError.imageLoadFailed(url.path) }

        var pixels = [EInkColor]()
        pixels.reserveCapacity(width * height)
        for offset in stride(from: 0, to: rgba.count, by: 4) {
            pixels.append(quantize(red: rgba[offset], green: rgba[offset + 1], blue: rgba[offset + 2]))
        }
        return try EInkFrame(width: width, height: height, pixels: pixels)
    }

    public func writePNG(to url: URL) throws {
        var rgba = [UInt8]()
        rgba.reserveCapacity(pixels.count * 4)
        for pixel in pixels {
            switch pixel {
            case .white: rgba.append(contentsOf: [255, 255, 255, 255])
            case .black: rgba.append(contentsOf: [0, 0, 0, 255])
            case .red: rgba.append(contentsOf: [198, 40, 40, 255])
            }
        }
        let data = Data(rgba) as CFData
        guard let provider = CGDataProvider(data: data),
              let image = CGImage(
                width: Self.width,
                height: Self.height,
                bitsPerComponent: 8,
                bitsPerPixel: 32,
                bytesPerRow: Self.width * 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                provider: provider,
                decode: nil,
                shouldInterpolate: false,
                intent: .defaultIntent
              ),
              let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
            throw EInkFrameError.imageWriteFailed(url.path)
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw EInkFrameError.imageWriteFailed(url.path)
        }
    }

    public func bitplanes() -> EInkBitplanes {
        var blackWhite = [UInt8]()
        var thirdColor = [UInt8]()
        blackWhite.reserveCapacity(15_000)
        thirdColor.reserveCapacity(15_000)

        for offset in stride(from: 0, to: pixels.count, by: 8) {
            var blackWhiteByte: UInt8 = 0
            var thirdColorByte: UInt8 = 0
            for bit in 0..<8 {
                blackWhiteByte <<= 1
                thirdColorByte <<= 1
                switch pixels[offset + bit] {
                case .white:
                    blackWhiteByte |= 1
                case .black:
                    break
                case .red:
                    thirdColorByte |= 1
                }
            }
            blackWhite.append(blackWhiteByte)
            thirdColor.append(thirdColorByte)
        }
        return EInkBitplanes(blackWhite: blackWhite, thirdColor: thirdColor)
    }
}
