import Foundation
import CryptoKit
import zlib

public enum StrictPNGError: Error { case invalidContainer, invalidChecksum, unsupportedEncoding, invalidPalette, invalidHash }

/// Native RGB bytes only: no color management, resizing, quantization or dithering.
/// Supports non-interlaced 8-bit RGB/RGBA/gray and 1/2/4/8-bit indexed/gray PNG.
public enum StrictPNG {
    public static func decode(_ data: Data, sha256: String) throws -> EInkFrame {
        guard data.count <= 1_048_576,
              SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == sha256 else { throw StrictPNGError.invalidHash }
        let bytes = [UInt8](data)
        guard bytes.count >= 33, Array(bytes.prefix(8)) == [137,80,78,71,13,10,26,10] else { throw StrictPNGError.invalidContainer }
        func u32(_ i: Int) -> Int { bytes[i..<i+4].reduce(0) { ($0 << 8) | Int($1) } }
        var offset = 8
        var depth = 0, color = -1, channels = 0
        var compressed = [UInt8](), palette = [UInt8](), transparency = [UInt8]()
        var seenHeader = false, seenEnd = false, seenData = false, dataEnded = false
        var seenPalette = false, seenTransparency = false
        while offset < bytes.count {
            guard offset + 12 <= bytes.count else { throw StrictPNGError.invalidContainer }
            let length = u32(offset)
            guard length <= bytes.count - offset - 12 else { throw StrictPNGError.invalidContainer }
            let nameBytes = Array(bytes[offset+4..<offset+8])
            guard nameBytes.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) }),
                  let name = String(bytes: nameBytes, encoding: .ascii) else { throw StrictPNGError.invalidContainer }
            let chunk = Array(bytes[offset+4..<offset+8+length])
            let checksum = chunk.withUnsafeBufferPointer { crc32(0, $0.baseAddress, uInt(chunk.count)) }
            guard Int(checksum) == u32(offset + 8 + length) else { throw StrictPNGError.invalidChecksum }
            let payload = Array(bytes[offset+8..<offset+8+length])
            guard seenHeader || name == "IHDR", !seenEnd else { throw StrictPNGError.invalidContainer }
            if seenData && name != "IDAT" { dataEnded = true }
            switch name {
            case "IHDR":
                guard !seenHeader, offset == 8, length == 13 else { throw StrictPNGError.invalidContainer }
                let width = u32(offset+8), height = u32(offset+12)
                guard width == 400, height == 300 else { throw EInkFrameError.invalidDimensions(width: width, height: height) }
                depth = Int(payload[8]); color = Int(payload[9])
                channels = [0: 1, 2: 3, 3: 1, 4: 2, 6: 4][color] ?? 0
                guard channels > 0, [1,2,4,8].contains(depth), (depth == 8 || color == 0 || color == 3), payload[10...12].allSatisfy({ $0 == 0 }) else { throw StrictPNGError.unsupportedEncoding }
                seenHeader = true
            case "PLTE":
                guard !seenPalette, !seenData, length > 0, length <= 768, length % 3 == 0, color != 0, color != 4 else { throw StrictPNGError.invalidContainer }
                palette = payload; seenPalette = true
            case "tRNS":
                guard !seenTransparency, !seenData, [0,2,3].contains(color), color != 3 || seenPalette else { throw StrictPNGError.invalidContainer }
                transparency = payload; seenTransparency = true
            case "IDAT":
                guard !dataEnded, color != 3 || seenPalette else { throw StrictPNGError.invalidContainer }
                compressed += payload; seenData = true
            case "IEND":
                guard length == 0, seenData, offset + 12 == bytes.count else { throw StrictPNGError.invalidContainer }
                seenEnd = true
            case "acTL", "fcTL", "fdAT": throw StrictPNGError.unsupportedEncoding
            default:
                guard nameBytes[0] & 32 != 0 else { throw StrictPNGError.unsupportedEncoding }
            }
            offset += length + 12
        }
        guard seenEnd, !compressed.isEmpty else { throw StrictPNGError.invalidContainer }
        if seenTransparency {
            guard (color == 0 && transparency.count == 2) || (color == 2 && transparency.count == 6) ||
                    (color == 3 && !transparency.isEmpty && transparency.count <= palette.count / 3) else { throw StrictPNGError.invalidContainer }
        }
        let rowLength = (400 * channels * depth + 7) / 8
        let bpp = max(1, (channels * depth + 7) / 8)
        let expected = 300 * (rowLength + 1)
        var inflated = [UInt8](repeating: 0, count: expected)
        var inflatedLength = uLongf(expected)
        let rc = uncompress(&inflated, &inflatedLength, compressed, uLong(compressed.count))
        guard rc == Z_OK, inflatedLength == expected else { throw StrictPNGError.invalidContainer }
        var prior = [UInt8](repeating: 0, count: rowLength)
        var pixels: [EInkColor] = []
        pixels.reserveCapacity(120_000)
        for y in 0..<300 {
            let start = y * (rowLength + 1)
            let filter = inflated[start]
            guard filter <= 4 else { throw StrictPNGError.invalidContainer }
            var row = Array(inflated[start+1..<start+1+rowLength])
            for i in 0..<rowLength {
                let a = i >= bpp ? row[i-bpp] : 0
                let b = prior[i]
                let c = i >= bpp ? prior[i-bpp] : 0
                let prediction: UInt8
                switch filter {
                case 1: prediction = a
                case 2: prediction = b
                case 3: prediction = UInt8((Int(a) + Int(b)) / 2)
                case 4:
                    let p = Int(a) + Int(b) - Int(c)
                    let pa = abs(p - Int(a)), pb = abs(p - Int(b)), pc = abs(p - Int(c))
                    prediction = pa <= pb && pa <= pc ? a : pb <= pc ? b : c
                default: prediction = 0
                }
                row[i] = row[i] &+ prediction
            }
            for x in 0..<400 {
                var r: UInt8, g: UInt8, b: UInt8
                var alpha: UInt8 = 255
                if color == 0 || color == 3 {
                    let sample = Int((row[x * depth / 8] >> (8 - depth - x * depth % 8)) & UInt8((1 << depth) - 1))
                    if color == 3 {
                        guard sample * 3 + 2 < palette.count else { throw StrictPNGError.invalidPalette }
                        r = palette[sample*3]; g = palette[sample*3+1]; b = palette[sample*3+2]
                        if sample < transparency.count { alpha = transparency[sample] }
                    } else {
                        r = UInt8(sample * 255 / ((1 << depth) - 1)); g = r; b = r
                        if transparency.count == 2 && sample == Int(transparency[0]) * 256 + Int(transparency[1]) { alpha = 0 }
                    }
                } else {
                    let i = x * channels
                    r = row[i]
                    if color == 4 { g = r; b = r; alpha = row[i+1] }
                    else {
                        g = row[i+1]; b = row[i+2]
                        if color == 6 { alpha = row[i+3] }
                        else if transparency == [0,r,0,g,0,b] { alpha = 0 }
                    }
                }
                guard alpha == 255 else { throw StrictPNGError.invalidPalette }
                switch (r,g,b) {
                case (255,255,255): pixels.append(.white)
                case (0,0,0): pixels.append(.black)
                case (198,40,40): pixels.append(.red)
                default: throw StrictPNGError.invalidPalette
                }
            }
            prior = row
        }
        return try EInkFrame(width: 400, height: 300, pixels: pixels)
    }
}
