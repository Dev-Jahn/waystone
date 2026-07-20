# 0.12 invariant traceability matrix

이 matrix는 `docs/invariants.md`의 I-01~I-12·E-01~E-09와
`docs/adr/ADR-0003-run-observability-and-cancellation.md`의
`취소, quiescence, cleanup 안전 계약`에 정의된 독립 취소·quiescence 안전 계약을 test layer에
결속한다. 현재 실물 test는 전부
`scripts/tests/run_tests.py`에 있다. `waystone/` 신규 package와 M1 kernel이 아직 없으므로
**새 kernel test 열은 전부 `TODO(M1)`**이다.

표의 characterization/fault-injection 셀은 test 이름이 아닌 본문의 fixture 조작과
assertion을 대조해 추린 현재 coverage다. 손상·race·partial write·drift·tool failure를
실제로 주입하는 method만 fault-injection 열에 놓았다. 현 0.11 test가 0.12의
SQLite/action protocol 전체를 증명하지는 않으므로, 실물이 있어도 상태는 `partial`이다.

| invariant ID | characterization test | 새 kernel test | fault-injection test | 상태 |
|---|---|---|---|---|
| I-01 | `AcceptFieldTests.test_accept_add_repeats_round_trips_and_packet_records_provenance`<br>`DelegateVerdictTests.test_g2_requires_exact_criterion_set`<br>`BasePolicyTests.test_policy_read_from_base_not_head` | TODO(M1) | `AcceptFieldTests.test_claim_rejects_dependency_drift_after_prepare` | partial — owner acceptance/provenance·drift refusal은 있으나 신 planner kernel 미구현 |
| I-02 | `DelegateApplyTests.test_apply_requires_verdict_and_forbids_no_verdict_override`<br>`DelegateVerdictTests.test_g3_with_verifier_requires_verify_artifact_and_records_binding`<br>`DelegateVerifyTests.test_success_normalizes_committed_delegate_and_preserves_labels` | TODO(M1) | TODO(M1) | partial — worker 자기-수용 금지와 별도 verifier는 있으나 kernel fault coverage 없음 |
| I-03 | `L3GapClosureAcceptanceTests.test_f1_run_records_patch_digest_and_apply_rejects_post_verdict_replacement`<br>`L3GapClosureAcceptanceTests.test_f1_verify_proves_base_plus_patch_matches_result_and_pre_digest_cannot_apply` | TODO(M1) | `L3GapClosureAcceptanceTests.test_f1_run_records_patch_digest_and_apply_rejects_post_verdict_replacement` | partial — patch bytes/digest tamper refusal 존재, submit kernel 미구현 |
| I-04 | `DelegateVerdictTests.test_g3_with_verifier_requires_verify_artifact_and_records_binding`<br>`DelegateVerifyTests.test_success_normalizes_committed_delegate_and_preserves_labels` | TODO(M1) | `DelegateVerifyTests.test_verifier_worktree_mutation_is_fail_loud_and_records_no_artifact` | partial — verifier artifact·decision 분리의 legacy coverage 존재 |
| I-05 | `DelegateSnapshotTests.test_live_tree_and_index_unchanged`<br>`DelegateApplyTests.test_apply_unrelated_dirty_ok` | TODO(M1) | `DelegateApplyTests.test_apply_drift_is_atomic_exit1` | partial — live tree/drift atomicity는 있으나 신 integration/delivery kernel 미구현 |
| I-06 | `DelegateRunTests.test_same_second_did_gets_suffix`<br>`DelegateVerdictTests.test_verdict_numbering_never_overwrites_and_state_does_not_change`<br>`DelegateVerifyTests.test_verify_artifact_name_collision_never_overwrites` | TODO(M1) | `DelegateVerifyTests.test_verify_artifact_name_collision_never_overwrites` | partial — legacy record/artifact non-overwrite는 있으나 transition audit kernel 미구현 |
| I-07 | `MigrationV2Phase2Tests.test_phase2_is_self_extinguishing_and_second_run_changes_nothing` | TODO(M1) | `MigrationV2Phase2Tests.test_profile_seed_recovers_after_atomic_replace_commits_then_raises`<br>`MigrationV2Phase2Tests.test_file_move_recovers_after_atomic_replace_commits_then_raises`<br>`MigrationV2Phase2Tests.test_symlinked_project_state_is_rejected_without_external_write` | partial — idempotence·partial write·symlink refusal은 있으나 DB migration kernel 미구현 |
| I-08 | `OverlayStoreTests.test_add_creates_observing_delta`<br>`OverlayStoreTests.test_promote_requires_replay`<br>`L2DPolicyMachineTests.test_user_promotion_is_explicit_and_evidence_gated`<br>`L2DPolicyMachineTests.test_consent_log_and_materialization_require_explicit_acceptance` | TODO(M1) | `L2DAdversarialFindingTests.test_f9_degraded_maturity_snapshot_never_records_a_transition` | partial — observe/promotion/consent legacy coverage; 신 runtime policy kernel 미구현 |
| I-09 | `DelegateCorruptRecordTests.test_status_list_marks_corrupt_row_and_keeps_healthy`<br>`DelegateCorruptRecordTests.test_apply_corrupt_contract_exit1` | TODO(M1) | `DelegateCorruptRecordTests.test_apply_corrupt_contract_exit1`<br>`L2DAdversarialFindingTests.test_f9_degraded_maturity_snapshot_never_records_a_transition` | partial — fail-toward-verification legacy coverage; core/store typed error 미구현 |
| I-10 | TODO(M1) | TODO(M1) | TODO(M1) | gap — M0 exit에서 공개 인지된 이월(TODO M1). `ImproveL2BAdversarialTests.test_f12_scope_is_structured_and_packet_text_is_never_mined`는 scope 근접 증거일 뿐 minimal worker prompt를 단언하지 않음 |
| I-11 | `DelegateProfileTests.test_schema_valid_but_unimplemented_execution_fails_loud`<br>`DelegateVerifyTests.test_unimplemented_execution_and_entry_fail_loud` | TODO(M1) | `DelegateRunTests.test_codex_sandbox_probe_failure_records_failed_env_without_main_runner` | partial — unsupported execution/probe failure의 silent fallback 금지 존재 |
| I-12 | `ContractInjectTests.test_routing_policy_renders_all_axes_questions_and_is_bounded`<br>`ContractInjectTests.test_contract_has_its_own_1300_character_cap`<br>`M2DocsTests.test_delegate_report_summarizes_warnings_without_internal_delta_ids` | TODO(M1) | TODO(M1) | partial — public-surface boundedness/friendly report만 coverage; 신 CLI/read API 미구현 |
| E-01 | `PacketPublicationTests.test_rendered_request_exposes_self_digest_and_canonicalizer_is_header_bounded`<br>`PacketPublicationTests.test_delayed_echo_stamps_named_generation_and_stays_pending_after_reprepare`<br>`PacketPublicationTests.test_pr_freeze_posts_the_exact_digest_verified_request_bytes` | TODO(M1) | `PacketPublicationTests.test_reprepare_crash_after_each_projection_write_stays_pending` | partial — rendered digest/echo·partial write fail-closed 존재 |
| E-02 | `PendingReviewTests.test_pending_is_derived_from_request_and_ingest_header_not_file_existence`<br>`IngestTests.test_stored_metadata_reader_projects_verbatim_body_header_only` | TODO(M1) | `PacketPublicationTests.test_feedback_cache_digest_edit_cannot_reassign_verbatim_reply` | partial — verbatim read-time derivation/cache tamper coverage 존재 |
| E-03 | `CodexRunnerVerificationGateTests.test_runtime_fingerprint_records_all_bounded_axes`<br>`CodexRunnerVerificationGateTests.test_darwin_unobserved_process_context_state_equivalent_marker_skips_probe` | TODO(M1) | `CodexRunnerVerificationGateTests.test_codex_config_content_change_reprobes_without_directory_stat_change`<br>`CodexRunnerVerificationGateTests.test_observed_and_unobserved_process_context_transitions_reprobe` | partial — proof exact-match/not-observed coverage; 신 probe table 미구현 |
| E-04 | TODO(M1) | TODO(M1) | TODO(M1) | gap — M0 exit에서 공개 인지된 이월(TODO M1). 현 round/exposure는 신규 sole-authority git-tracked closeout manifest가 아님 |
| E-05 | `DelegateRunTests.test_same_second_did_gets_suffix`<br>`DelegateVerdictTests.test_verdict_numbering_never_overwrites_and_state_does_not_change`<br>`DelegateVerdictTests.test_g2_requires_exact_criterion_set` | TODO(M1) | `DelegateVerifyTests.test_verify_artifact_name_collision_never_overwrites` | partial — legacy append-only/criterion binding만 있고 CAS current-state row + transitions audit는 없음 |
| E-06 | `DelegateCorruptRecordTests.test_status_list_marks_corrupt_row_and_keeps_healthy` | TODO(M1) | `DelegateCorruptRecordTests.test_status_list_marks_corrupt_row_and_keeps_healthy` | partial — record 격리는 있으나 SQLite/artifact-reference 복구·digest 재검증 미구현 |
| E-07 | `L3GapClosureAcceptanceTests.test_f1_verify_proves_base_plus_patch_matches_result_and_pre_digest_cannot_apply`<br>`DelegateVerifyTests.test_success_normalizes_committed_delegate_and_preserves_labels` | TODO(M1) | `L3GapClosureAcceptanceTests.test_f1_apply_rechecks_contract_and_verify_artifact_digests`<br>`L3GapClosureAcceptanceTests.test_f1_run_records_patch_digest_and_apply_rejects_post_verdict_replacement` | partial — exact result digest/tamper coverage; 신 verification/integration kernel 미구현 |
| E-08 | TODO(M1) | TODO(M1) | TODO(M1) | gap — M0 exit에서 공개 인지된 이월(TODO M1). 현 review/statusline의 `unknown`은 run liveness·progress·current 증거가 아님 |
| E-09 | `DelegatePacketDigestTests.test_digest_is_deterministic_and_worktree_stable`<br>`StoragePathTests.test_machine_dir_is_host_neutral_and_honors_override` | TODO(M1) | `CodexRunnerVerificationGateTests.test_codex_config_content_change_reprobes_without_directory_stat_change` | partial — intrinsic digest/content-change coverage; 신 attribution adapter 미구현 |
| ADR-0003 `취소, quiescence, cleanup 안전 계약` | TODO(M1) | TODO(M1) | TODO(M1) | gap — independent row required; positive process/effect reconciliation + observed-quiescent + separate cleanup test 없음 |

## 취소·cleanup 역-계약 legacy test

다음 test는 위 독립 행의 coverage가 아니다. 현재 본문이 `running`, unreadable record,
또는 record 부재를 positive quiescence/effect reconciliation으로 증명하지 않고 worktree/ref
삭제의 근거로 삼기 때문이다. `docs/porting-ledger.md`에서는 모두 `rewrite`로 처분했다.

- `DelegateApplyTests.test_discard_cleanup_and_accepts_running`
- `DelegateApplyTests.test_discard_orphan_cleans_refs_and_cache_without_record`
- `DelegateApplyTests.test_discard_records_intent_and_resumes_with_new_or_inherited_reason`
- `DelegateApplyTests.test_discard_orphan_is_project_locked_and_cleanup_failures_are_loud`
- `DelegateCorruptRecordTests.test_discard_accepts_corrupt_record`
- `DelegateCorruptRecordTests.test_owner_lock_scan_fail_safe_on_corrupt_status` — fail-safe 차단은
  보존하되 `discard`를 복구 안내로 고정한 diagnostic을 reconcile/expert recovery로 재작성.
- `DelegateRunTests.test_claim_only_crash_remnant_is_discardable` — claim-only 잔여물은
  fencing epoch 미진행과 해당 action id가 각인된 worktree·ref·process·artifact의 부재 관측을
  모두 충족할 때만 폐기한다. 관측 채널이 없으면 `unknown-effect`로 보존한다
  (`tasks.yaml`의 `decision/claim-only-remnant-discard-proof`).

또한
`CodexRunnerVerificationGateTests.test_fixed_stdout_shim_replacement_reprobes_via_executable_stat_identity`는
동일 version 출력에서 executable size/mtime만으로 proof를 무효화한다. 완료 ruling에 따라 교체
판정은 executable content digest에 결속하며, size/mtime은 값싼 사전 필터로만 허용하고 단독 판정
근거로 쓰지 않는다 (`tasks.yaml`의 `decision/shim-replacement-identity-basis`).
