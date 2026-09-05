import Foundation

@MainActor class NativeTestCase {
    var cleanup: [() throws -> Void] = []
    func addTeardownBlock(_ block: @escaping () throws -> Void) { cleanup.append(block) }
    func tearDown() { for action in cleanup { try? action() }; cleanup.removeAll() }
}
func XCTFail(_ message: String = "assertion failed", file: StaticString = #filePath, line: UInt = #line) -> Never {
    fatalError("\(file):\(line): \(message)")
}
func XCTAssertTrue(_ expression: @autoclosure () throws -> Bool, file: StaticString = #filePath, line: UInt = #line) {
    do { if try !expression() { XCTFail("expected true", file: file, line: line) } } catch { XCTFail("\(error)", file: file, line: line) }
}
func XCTAssertFalse(_ expression: @autoclosure () throws -> Bool, file: StaticString = #filePath, line: UInt = #line) { XCTAssertTrue(try !expression(), file: file, line: line) }
func XCTAssertEqual<T: Equatable>(_ a: @autoclosure () throws -> T, _ b: @autoclosure () throws -> T, file: StaticString = #filePath, line: UInt = #line) {
    do { let lhs = try a(), rhs = try b(); if lhs != rhs { XCTFail("expected \(rhs), got \(lhs)", file: file, line: line) } } catch { XCTFail("\(error)", file: file, line: line) }
}
func XCTAssertNil<T>(_ value: @autoclosure () throws -> T?, file: StaticString = #filePath, line: UInt = #line) { XCTAssertTrue(try value() == nil, file: file, line: line) }
func XCTAssertNotNil<T>(_ value: @autoclosure () throws -> T?, file: StaticString = #filePath, line: UInt = #line) { XCTAssertTrue(try value() != nil, file: file, line: line) }
func XCTAssertThrowsError<T>(_ expression: @autoclosure () throws -> T, file: StaticString = #filePath, line: UInt = #line) {
    do { _ = try expression() } catch { return }; XCTFail("expected error", file: file, line: line)
}
func XCTAssertNoThrow<T>(_ expression: @autoclosure () throws -> T, file: StaticString = #filePath, line: UInt = #line) {
    do { _ = try expression() } catch { XCTFail("unexpected \(error)", file: file, line: line) }
}
func XCTUnwrap<T>(_ value: T?, file: StaticString = #filePath, line: UInt = #line) throws -> T {
    guard let value else { XCTFail("unexpected nil", file: file, line: line) }; return value
}

@main struct NativeTests {
    @MainActor static func main() async throws {
        try LoginTests().testNoArgumentLaunchRestoresSelectedDirectoryAndSavedState()
        try LoginTests().testFreshInstallAndExplicitDirectoryDoNotChangeStartupSelection()
        try LoginTests().testDamagedSelectionAndMissingTargetBlockRecoveryWithoutOverwrite()
        try LoginTests().testSavedSelectionRestoresUnpausedSettingsAndRejectsBadWrites()
        let c = ContractTests(), p = PNGTests(), h = HostTests(), cross = CrossBridgeTests()
        defer { c.tearDown(); p.tearDown(); h.tearDown(); cross.tearDown() }
        let tests: [(String, () throws -> Void)] = [
            ("settings schema/defaults", c.testFirstLaunchNeverEnablesSyncAndWritesExactSchemaIncludingNullBinding),
            ("settings corruption", c.testCorruptUnknownVersionOrInconsistentSettingsAreNotOverwritten),
            ("binding revisions", c.testOnlyQuiescentValidatedBindingCanIncreaseRevision),
            ("single owner", c.testSingleOwnerLockExcludesAnotherInstanceAndReleasesOnClose),
            ("JSONL boundary", c.testJSONLFragmentationAndOneMiBIncludingNewline),
            ("message schema", c.testProtocolRejectsWrongTypesExtraTargetAndUnknownVersion),
            ("strict JSON", c.testProtocolRejectsDuplicateKeysDeepNestingAndNonFiniteNumbers),
            ("correlation", c.testCorrelationRejectsConcurrentDuplicateAndStaleCallbacks),
            ("send receipts", c.testSendResultUsesExactContractAndRequiresRealDisconnectEvidence),
            ("setup gate", c.testSetupReadinessCannotBeFakedByEnabledSettingsOrFailedInspector),
            ("pause race retryability", c.testPauseRaceKeepsPendingRequestRetryableInsteadOfCreatingPermanentBindingFailure),
            ("transient in-flight inspection", c.testRequestAfterTransientInspectionPauseIsRetryable),
            ("worker start gate", c.testWorkerNeverStartsBeforeTheCompleteSendGateIsOpen),
            ("incomplete legacy inspection wording", c.testIncompleteSetupNeverClaimsLegacyIsAbsent),
            ("restart quiescence", c.testWorkerRestartWaitsForBothProcessExitAndBluetoothQuiescence),
            ("explicit-only recovery", c.testExplicitRetryResumesEvenIfWorkerAlreadyRunningButAutomaticRestartCannotResetPermanentFailure),
            ("resume retry contract", c.testResumeRetryIsAnExplicitBooleanNeverAddedToAutomaticResume),
            ("duplicate ready", c.testDuplicateReadyAndOldSessionReadyAreRejected),
            ("serial transfer", c.testSerialWriteCompletionNeedsAllCallbacksAndDisconnect),
            ("cleanup timeout", c.testEarlyDisconnectAndCleanupTimeoutNeverBecomeSuccess),
            ("strict PNG palette/hash", p.testAcceptsExactNativeRedAndRejectsGrayTransparencyWrongSizeAndHash),
            ("PNG container", p.testRejectsCorruptContainerTrailingBytesAndWrongSignatureEvenWithMatchingSHA),
            ("frozen sample", p.testFrozenSampleDecodesWithoutQuantizationOrPixelChanges),
            ("helper arguments", h.testHelperArgumentsAreNamedAndPreviewRequiresValidatedSource),
        ]
        for (name, test) in tests { try test(); print("PASS \(name)") }
        try await h.testChildFragmentedReadyThenEOFClosesSessionWithoutUIOrBluetooth()
        print("PASS child EOF")
        try await h.testStopClosesIdleChildStdinAndAwaitsTermination()
        print("PASS child stop")
        try await h.testOneShotHelperBoundsOutputAndReportsNonzeroWithoutLeakingStderr()
        print("PASS bounded helper")
        try await h.testEOFInvalidatesSessionBeforeChildTerminationSoBLECanCancelImmediately()
        print("PASS EOF cancellation ordering")
        try await h.testRealChildMalformedOrWrongSessionOutputClosesProtocol()
        print("PASS invalid child protocol")
        try await h.testWorkerPipeCarriesExplicitRetryAndExactReceiptToChild()
        print("PASS real pipe roundtrip")
        try await h.testUnresponsiveChildCannotBlockMainActorWritingControls()
        print("PASS bounded control writes")
        LoginTests().testSystemStatusMappingSeparatesApprovalFromEnabledAndExternalDisable()
        print("PASS system login status mapping")
        try await cross.testRealBridgeStartsPausedThenCommitsOnlyCompleteMockBLEReceipt()
        print("PASS native host / Python bridge / validated renderer / mock BLE journal commit")
        try await cross.testRealBridgeAutomaticRestartPreservesPermanentErrorUntilExplicitRetry()
        print("PASS cross-session permanent error and explicit retry")
        PresentationTests().testSettingsNavigationUsesFourOperatorJobsWithOverviewFirst()
        try PresentationTests().testOnlyDamagedSettingsRequireTheSettingsRepairInstructions()
        print("PASS settings-specific repair guidance")
        PresentationTests().testFreshDefaultPrefersSystemPythonWithoutOverridingExplicitSelection()
        print("PASS system Python default and saved choice preservation")
        try await h.testDirectTerminationInvalidatesSessionAndBoundsDescendantPipeDrain()
        print("PASS direct exit / descendant stdout / BLE quiescence ordering")
        try await h.testStopAfterDirectExitDoesNotWaitForDescendantEOFOrDuplicateCallbacks()
        print("PASS stop with descendant-held stdout")
        try await RecoveryTests().testPeriodicInspectionRecoversWithoutManualRetryAndPauseActionResumes()
        try await RecoveryTests().testPeriodicRecoveryPreservesPauseUntilUserResumes()
        let preflight = SendPreflightTests()
        try await preflight.testChangedSetupAfterCachedReadyPreventsBLEStartUsingFreshRealPipeInspection()
        await preflight.testBusyOrFailedInspectorNeverFallsBackToCachedReady()
        await preflight.testSessionRequestBindingAndPauseAreRecheckedAfterAwait()
        await preflight.testFreshInspectionBindsInstallationAndPreservesBridgeDeadline()
        await preflight.testTimedOutRealInspectorProcessCannotStartBLE()
        await preflight.testReadOnlyInspectorTimeoutIsBoundedWhenDescendantHoldsStdout()
        await preflight.testTransientInspectionFailuresRetryButConflictsRemainBlocked()
        print("PASS fresh setup preflight / busy and stale identity / total deadline")
        print("PASS 52 native tests (no app launch or Bluetooth)")
    }
}
