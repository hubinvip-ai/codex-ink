import Foundation

struct TestFailure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else { throw TestFailure(message) }
}

func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String) throws {
    guard actual == expected else { throw TestFailure("\(message): expected \(expected), got \(actual)") }
}

do {
    try runFrameTests()
    try runProtocolTests()
    print("PASS 2 suites")
} catch {
    fputs("FAIL \(error)\n", stderr)
    exit(1)
}
