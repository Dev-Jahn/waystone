#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Aggregate entry point for the mechanically split Waystone test suite.

Run: uv run scripts/tests/run_tests.py [Class[.method] ...]
"""
from __future__ import annotations

import unittest

from test_delegate_cli import (
    DelegateFanoutPlanTests,
    DelegatePacketDigestTests,
    DelegateExpectAndCarrierTests,
    DelegateJsonEventsTests,
    DelegateStatusJsonTests,
    DelegateFanoutTemplateLintTests,
    DelegateMainContractTests,
    DelegateCliTests,
)
from test_delegate_core import (
    DelegateSnapshotTests,
    DelegateProfileTests,
    DelegatePacketTests,
    DelegateRunTests,
    CodexRunnerVerificationGateTests,
)
from test_delegate_lifecycle import (
    DelegateEffortTests,
    DelegateApplyTests,
    DelegateVerdictTests,
    DelegateCorruptRecordTests,
)
from test_delegate_verify import (
    DelegateVerifyTests,
    UvCacheTests,
    ContractInjectTests,
    CodexVerifierTests,
)
from test_improve import (
    CclogParseTests,
    CclogLayoutTests,
    ImproveDiscoveryTests,
    ImproveTraceTests,
    ImproveSelfSessionTests,
    ImproveReviewsTests,
    ImproveAuditTests,
    ImproveDecideTests,
    ImproveMetricsTests,
    ImproveScopeTests,
    ImproveM1DefectTests,
    EvidenceTests,
    ImproveL2BTests,
    ImproveL2BAdversarialTests,
    CodexTraceTests,
    L2CImproveFeedbackTests,
)
from test_migrations import (
    MigrationSunsetTests,
    MigrationV2HookTests,
    MigrationTests,
    M2DocsTests,
    CodexHookTests,
)
from test_overlay import (
    OverlayStoreTests,
    OverlayRuleTests,
    BoundaryWarnTests,
    DelegateExposureOverlayTests,
    ReplayTests,
)
from test_policy import (
    L2CGuardTests,
    L2CAdversarialFixTests,
    L2DPolicyMachineTests,
    L2DAdversarialFindingTests,
    CodexPluginContractTests,
    L3GapClosureAcceptanceTests,
)
from test_project import (
    ResumeStartHereTests,
    StoragePathTests,
    DashboardLockingTests,
    WaystoneStorageCliTests,
    ConfigTests,
    TextSurgeryTests,
    NextActionableTests,
    LaneTests,
    RoundCloseTests,
)
from test_release import (
    ReleaseToMainTests,
)
from test_review_protocol import (
    LockPrimitiveTests,
    LockWiringTests,
    MarkerTests,
    MergeGateTests,
    TasksGateTests,
    RemoteTests,
    PacketPublicationTests,
    RoundExposureTests,
)
from test_review_settlement import (
    BasePolicyTests,
    IngestTests,
    PendingReviewTests,
    StatuslineTests,
    FrozenAcceptanceTests,
    IntegrationSmokeTests,
)
from test_tasks import (
    TaskCliTests,
    UninitializedRootGateTests,
    TaskArchiveTests,
    ParkedTaskContractTests,
    TaskReadNudgeTests,
    TaskRegressionTests,
    AcceptFieldTests,
)

_TEST_CLASSES = (
    DelegateFanoutPlanTests,
    DelegatePacketDigestTests,
    DelegateExpectAndCarrierTests,
    DelegateJsonEventsTests,
    DelegateStatusJsonTests,
    DelegateFanoutTemplateLintTests,
    DelegateMainContractTests,
    DelegateCliTests,
    DelegateSnapshotTests,
    DelegateProfileTests,
    DelegatePacketTests,
    DelegateRunTests,
    CodexRunnerVerificationGateTests,
    DelegateEffortTests,
    DelegateApplyTests,
    DelegateVerdictTests,
    DelegateCorruptRecordTests,
    DelegateVerifyTests,
    UvCacheTests,
    ContractInjectTests,
    CodexVerifierTests,
    CclogParseTests,
    CclogLayoutTests,
    ImproveDiscoveryTests,
    ImproveTraceTests,
    ImproveSelfSessionTests,
    ImproveReviewsTests,
    ImproveAuditTests,
    ImproveDecideTests,
    ImproveMetricsTests,
    ImproveScopeTests,
    ImproveM1DefectTests,
    EvidenceTests,
    ImproveL2BTests,
    ImproveL2BAdversarialTests,
    CodexTraceTests,
    L2CImproveFeedbackTests,
    MigrationSunsetTests,
    MigrationV2HookTests,
    MigrationTests,
    M2DocsTests,
    CodexHookTests,
    OverlayStoreTests,
    OverlayRuleTests,
    BoundaryWarnTests,
    DelegateExposureOverlayTests,
    ReplayTests,
    L2CGuardTests,
    L2CAdversarialFixTests,
    L2DPolicyMachineTests,
    L2DAdversarialFindingTests,
    CodexPluginContractTests,
    L3GapClosureAcceptanceTests,
    ResumeStartHereTests,
    StoragePathTests,
    DashboardLockingTests,
    WaystoneStorageCliTests,
    ConfigTests,
    TextSurgeryTests,
    NextActionableTests,
    LaneTests,
    RoundCloseTests,
    ReleaseToMainTests,
    LockPrimitiveTests,
    LockWiringTests,
    MarkerTests,
    MergeGateTests,
    TasksGateTests,
    RemoteTests,
    PacketPublicationTests,
    RoundExposureTests,
    BasePolicyTests,
    IngestTests,
    PendingReviewTests,
    StatuslineTests,
    FrozenAcceptanceTests,
    IntegrationSmokeTests,
    TaskCliTests,
    UninitializedRootGateTests,
    TaskArchiveTests,
    ParkedTaskContractTests,
    TaskReadNudgeTests,
    TaskRegressionTests,
    AcceptFieldTests,
)

for _test_class in _TEST_CLASSES:
    _test_class.__module__ = __name__
del _test_class


if __name__ == "__main__":
    unittest.main(verbosity=2)
