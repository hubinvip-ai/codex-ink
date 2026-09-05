import Foundation

public enum EPDProtocolError: Error, Equatable {
    case invalidPlaneLength(expected: Int, blackWhite: Int, thirdColor: Int)
    case invalidChunkSize(Int)
    case offsetOverflow(Int)
}

public enum EPDProtocol {
    public static let planeByteCount = 15_000

    public static func commands(planes: EInkBitplanes, chunkSize: Int = 450) throws -> [Data] {
        guard planes.blackWhite.count == planeByteCount, planes.thirdColor.count == planeByteCount else {
            throw EPDProtocolError.invalidPlaneLength(
                expected: planeByteCount,
                blackWhite: planes.blackWhite.count,
                thirdColor: planes.thirdColor.count
            )
        }
        guard chunkSize > 0, chunkSize <= 508 else {
            throw EPDProtocolError.invalidChunkSize(chunkSize)
        }

        var commands = [Data([0x00, 0x00]), Data([0x02, 0x00, 0x00])]
        try appendPlane(planes.blackWhite, code: 0xFF, chunkSize: chunkSize, to: &commands)
        try appendPlane(planes.thirdColor, code: 0x00, chunkSize: chunkSize, to: &commands)
        commands.append(Data([0x01, 0x01]))
        return commands
    }

    private static func appendPlane(_ bytes: [UInt8], code: UInt8, chunkSize: Int, to commands: inout [Data]) throws {
        for offset in stride(from: 0, to: bytes.count, by: chunkSize) {
            guard offset <= Int(UInt16.max) else { throw EPDProtocolError.offsetOverflow(offset) }
            let end = min(offset + chunkSize, bytes.count)
            let encodedOffset = UInt16(offset)
            var packet = Data([
                0x03,
                code,
                UInt8((encodedOffset >> 8) & 0xFF),
                UInt8(encodedOffset & 0xFF),
            ])
            packet.append(contentsOf: bytes[offset..<end])
            commands.append(packet)
        }
    }
}
