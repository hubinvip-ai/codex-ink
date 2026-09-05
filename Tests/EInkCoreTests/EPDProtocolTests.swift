import Foundation
import EInkCore

func runProtocolTests() throws {
    let planes = EInkBitplanes(blackWhite: Array(repeating: 0xFF, count: 15_000), thirdColor: Array(repeating: 0x00, count: 15_000))
    let commands = try EPDProtocol.commands(planes: planes, chunkSize: 450)
    try expectEqual(commands.count, 71, "command count")
    try expectEqual(commands[0], Data([0x00, 0x00]), "reset command")
    try expectEqual(commands[1], Data([0x02, 0x00, 0x00]), "image-mode command")
    try expectEqual(Array(commands[2].prefix(4)), [0x03, 0xFF, 0x00, 0x00], "first black-white chunk header")
    try expectEqual(commands[2].count, 454, "full packet length")
    try expectEqual(Array(commands[3].prefix(4)), [0x03, 0xFF, 0x01, 0xC2], "second chunk offset")
    try expectEqual(Array(commands[36].prefix(4)), [0x03, 0x00, 0x00, 0x00], "first third-color chunk header")
    try expectEqual(commands.last, Data([0x01, 0x01]), "full-refresh command")

    do {
        _ = try EPDProtocol.commands(planes: EInkBitplanes(blackWhite: [0xFF], thirdColor: [0x00]), chunkSize: 450)
        throw TestFailure("invalid plane lengths were accepted")
    } catch is EPDProtocolError {
        // Expected.
    }
}
