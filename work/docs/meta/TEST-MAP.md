# 测试 ↔ 契约映射（TEST-MAP）

> **本文只做一件事**：把**契约条目编号**（`BASELINE.md` 的 **B1–B13 / L1–L9**、`ARCHITECTURE.md` 的
> **§2/§5/§6**、`test_rule_validation.py` 里的历史规则编号 **A5–A13**）逐个映射到**断言它的测试函数**。
>
> - **行为基线**（B/L 条目的含义）→ [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md)
> - **架构与模块契约**（§2/§5/§6 的含义）→ [`docs/contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md)
> - **实际运行记录、环境与历史采集数据**（E 编号）→ [`docs/meta/EVIDENCE.md`](EVIDENCE.md)
> - **命名结果对照、文件系统矩阵** → [`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §2/§3.1
>
> 本文**不重新论证**任何一条契约，只回答"这条契约由谁守着"。`COMPATIBILITY.md` §3.2 只保留一句指向这里。

---

## 0. 两套编号，别混

仓库里有两套彼此无关的字母编号，历史上曾被混用，是文档错标的根因：

| 编号体系 | 出处 | 形态 | 状态 |
|---|---|---|---|
| **B1–B13** | [`BASELINE.md`](../contracts/BASELINE.md) §4 | 现行为契约（"必须是什么样"） | **现行、唯一权威**。新增往后排，**不复用**旧号 |
| **A5–A13** | `tests/test_rule_validation.py` 的**函数名** | 历史"规则语料"编号（"当初第 N 条立项"），是测试留档名 | **只是名字**。改名会波及引用，故原样保留；含义以本文 §6 为准 |

`tests/test_rule_validation.py` 里还嵌着小写 `b1/b3/b6/b9`（如 `test_b6_engine_logs_live_in_the_data_volume`），
它们是**同一套历史语料的 B 组编号**，与 `BASELINE.md` 现在的 B6（日志格式冻结）**不是同一件事** ——
一律按本文 §6 的表读。

### 0.1 已修正的 B 编号错标（5 处）

`COMPATIBILITY.md` / `BASELINE.md` 旧版曾把下列 5 处编号写错，现已按 `BASELINE.md` §4 的**真值**修正；
真值口径就是现在的 B 表：

| 位置 | 旧标（错） | 真值（对） | 说明 |
|---|---|---|---|
| 旧 `COMPATIBILITY.md` §3.2 | `B5` = 日志格式冻结 | **B6** | B5 是源完整性保证，与日志格式无关 |
| 旧 `COMPATIBILITY.md` §3.2 | `B7` = 账本 `link_records` 是建链判据 | **B10** | B7 是"幂等只由体积一致提供"，是**粒度**；账本是**判据来源** |
| 旧 `COMPATIBILITY.md` §3.5 ④ | `B7` = 源完整性核对 | **B5** | 把两件事挤进同一个号 |
| 旧 `COMPATIBILITY.md` §3.5 ⑤ | `B8` = `LANG`/`PYTHONUTF8` 编码锁定 | **B9** | B8 是"绝不复制回退 + 三态预检" |
| 旧 `COMPATIBILITY.md` §3.5、旧 `BASELINE.md` 正文 | `B10` = `permission_mode` | **不在 B 表**；它是 [`BASELINE.md`](../contracts/BASELINE.md) **§1.6 + B13** | `permission_mode` 是库字段，不是建链判据 |

> 另有两处**跨文档**错标，属别的文档、不在本文件处置范围，仅在此点名备查：
> `docs/guides/DEPLOYMENT.md` 用 `B14` 指"硬链接日志逐字格式"（应为 **B6**）；
> `docs/guides/FAQ.md` 用 `B14` 指"每库作品账本"（应为 **B10**）。`B14` 不存在，本表不新设该号。

---

## 1. `BASELINE.md` §1 硬链接契约

| 契约点 | 测试 | 结论 |
|---|---|---|
| §1.1 入口与签名（含 `permission_mode`、`skip`） | `tests/test_hardlink_native.py::test_the_native_implementation_keeps_the_frozen_print_prefixes`、`tests/test_adapter_link.py::test_full_run_links_every_nested_file` | 签名与打印前缀冻结 |
| §1.2 日志格式与解析（恰好 3 段、按源目录隔离） | `tests/test_adapter_link.py::test_documented_log_line_per_first_level_folder`、`tests/test_file_log_isolation.py::test_bookkeeping_lines_are_written_verbatim` | 行格式逐字一致 |
| §1.3 孤儿清理（破坏性、可开关） | `tests/test_adapter_orphan.py::test_default_cleanup_removes_an_orphaned_target_folder`、`::test_orphan_cleanup_false_leaves_the_library_and_the_log_untouched`、`::test_orphan_cleanup_false_also_holds_on_the_ledger_path`、`::test_remove_log_entries_removes_only_the_named_folder`、`::test_remove_log_entries_with_a_wildcard_folder_drops_the_whole_source`、`::test_drop_and_restore_lines_preserve_the_file_byte_for_byte` | 默认清理、关闭后不删且日志不动 |
| §1.4 主流程 / 体积跳过 / 目录层级契约 | `tests/test_adapter_link.py` 全 10 条、`tests/test_idempotency.py` 全 9 条 | 一级目录遍历、层级逐字节保留、体积一致跳过 |
| §1.5 边界条件（已存在 / 跨挂载 / 空文件夹） | `tests/test_adapter_link.py::test_existing_target_file_fails_without_overwriting`、`::test_existing_target_hardlink_is_idempotent_not_a_failure`、`::test_empty_folder_is_logged_with_size_zero`、`::test_missing_source_dir_returns_an_exception_field` | 不覆盖、不复制、失败不中断 |
| §1.6 权限模式（默认关闭、只动目标侧） | `tests/test_permission_mode.py` 全 9 条 | 见 §4 的 B13 |
| §1.7 默认路径对 | 无（纯文档化的示例值） | — |

## 2. `BASELINE.md` §2–§3 重命名契约

| 契约点 | 测试 | 结论 |
|---|---|---|
| §2.2 缓存日志格式与跳过 | `tests/test_library_logs.py::test_json_indent_matches_the_original_script`、`::test_written_bytes_are_byte_identical_to_the_original_dump_parameters`、`::test_read_json_cache_returns_entries_with_unknown_keys_intact`、`::test_write_is_atomic_and_leaves_no_tmp_debris`、`tests/test_processor_engine.py::test_second_run_reports_the_skip_record` | 落盘字节与旧实现一致；未知键必须容忍 |
| §2.3 季文件夹 `fullmatch` | `tests/test_processor_engine.py::test_accept_dirs_use_fullmatch_not_search`、`::test_skip_season_zero_is_honoured_when_a_season_group_is_present` | 只接受恰好 `Season <数字>`；`Season 0` 跳过 |
| §2.4 集数提取（九条规则 / `legacy` 旧口径） | `tests/test_bangumi_episode_rules.py` 全 4 条、`tests/test_processor_engine.py::test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none`、`::test_an_unresolved_episode_keeps_the_original_name` | 规则顺序与"认不出就保留原名" |
| §2.5 主流程 | `tests/test_processor_engine.py::test_expressions_are_evaluated_in_written_order`、`::test_reject_dirs_block_the_whole_subtree_before_any_parsing`、`::test_title_scope_reads_files_from_the_title_folder_itself`、`::test_subdir_scope_requires_a_direct_child_and_a_whitelisted_lowercase_extension` | 作用域与遍历顺序 |
| §2.6 输出命名规则（冻结） | `tests/test_rename_extensions.py` 全 5 条、`tests/test_processor_engine.py::test_segments_support_param_literal_and_pad`、`::test_pad_only_adds_zeros_and_never_truncates`、`::test_segments_rendering_an_empty_name_skip_the_file_with_a_chinese_reason` | 段序渲染、`suffix` 算法、空名跳过 |
| §3 电影重命名（每后缀取最大） | `tests/test_processor_engine.py::test_selection_per_extension_keeps_only_the_largest_file_per_suffix`、`::test_selection_per_file_processes_every_match`、`::test_the_movie_preset_does_not_skip_when_there_is_no_episode` | 取舍规则 |

## 3. `BASELINE.md` §4 实现契约 B1–B13

| # | 契约 | 测试 | 结论 |
|---|---|---|---|
| **B1** | 容器自包含，不挂宿主目录 | `tests/test_rule_validation.py::test_b1_no_original_script_dir_is_mounted_and_no_copy_ships_in_the_image`、`::test_b1_no_original_script_dir_setting_is_needed` | compose/Dockerfile 文本断言 |
| **B2** | 重命名与硬链接都由应用自己代码执行 | `tests/test_hardlink_native.py::test_the_runtime_module_is_the_applications_own_implementation`、`::test_runtime_creates_real_hard_links_without_any_script`、`::test_no_reference_copy_or_loader_survives_in_the_app`、`::test_the_native_implementation_keeps_the_frozen_print_prefixes` | 运行期模块 = 应用自身实现 |
| **B3** | 判据产物冻结成黄金值 | `tests/test_hardlink_native.py` 的 A 组五条：`::test_the_native_implementation_on_a_fresh_tree`、`::test_the_native_implementation_when_already_logged`、`::test_the_native_implementation_when_a_folder_size_changes`、`::test_the_native_implementation_when_a_source_folder_disappears`、`::test_the_native_implementation_on_a_damaged_log`；+ `tests/test_processor_spec.py::test_module_constants_match_the_frozen_contract`、`tests/test_processor_engine.py::test_outcome_to_dict_covers_the_documented_keys` | 行为没有漂移的核心证据 |
| **B4** | `default` 相对 `legacy` 只允许四处增量 | `tests/test_processor_spec.py::test_the_builtin_bangumi_preset_pair_differs_only_in_the_registered_increments`、`tests/test_bangumi_episode_rules.py::test_default_preset_declares_the_nine_named_rules_in_order`、`tests/test_processor_engine.py::test_the_builtin_bangumi_default_preset_keeps_the_original_name_when_unresolved`、`tests/test_extras_classification.py::test_executor_does_not_claim_extras_when_l3_is_off` | 增量可枚举；防丢与特典排除**不是**预设差异 |
| **B5** | 源目录不被写 + 源完整性核对 | `tests/test_no_source_mutation.py` 全 8 条（含 `::test_source_directory_may_be_mounted_read_only`、`::test_destructive_write_sites_are_pinned_and_rename_only_touches_the_target_library`）、`tests/test_paths.py::test_snapshot_and_verify_source_tree_report_added_removed_and_changed`、`::test_verify_source_tree_ignores_nlink_and_ctime_so_hardlinks_never_false_positive`、`::test_verify_source_tree_never_modifies_anything`、`tests/test_hardlink_precheck.py::test_executor_reports_a_tampered_source_without_failing_the_task`、`::test_executor_marks_a_clean_source_as_intact_after_a_real_run` | 只读源目录可用；`nlink`/`ctime` 不误报 |
| **B6** | 日志格式冻结（硬链接纯文本 / 重命名 JSON） | `tests/test_library_logs.py::test_written_bytes_are_byte_identical_to_the_original_dump_parameters`、`tests/test_adapter_link.py::test_documented_log_line_per_first_level_folder`、`tests/test_processor_engine.py::test_log_entry_has_size_timestamp_and_works_dir`、`::test_pre_existing_log_entries_with_unknown_keys_are_tolerated` | 两种格式都逐字冻结 |
| **B7** | 幂等只由"体积一致 ⇒ 跳过"提供 | `tests/test_idempotency.py` 全 9 条（`::test_second_run_is_a_pure_noop`、`::test_repeated_runs_never_rebuild_links`、`::test_process_restart_still_skips`、`::test_empty_folder_stays_skipped_forever`…） | 判据**粒度**不变（来源见 B10） |
| **B8** | 绝不复制回退 + 三态预检（警告不硬拦） | `tests/test_rule_validation.py::test_a9_no_copy_fallback_anywhere_in_the_backend`、`tests/test_paths.py::test_paths_module_never_copies_files`、`tests/test_hardlink_precheck.py::test_the_precheck_module_never_copies_files`、`tests/test_adapter_link.py::test_cross_filesystem_warns_but_still_runs_the_script_without_copying`、`::test_every_target_file_is_a_real_hardlink_never_a_copy`、`tests/test_paths.py` 探针组 13 条（`::test_probe_reports_exdev_as_a_reliable_false`、`::test_check_same_mount_admits_it_is_necessary_not_sufficient`、`::test_check_hardlink_support_marks_a_fallback_verdict_as_unproven`…） | 跨挂载只警告、产物必须是真硬链接 |
| **B9** | `LANG`/`PYTHONUTF8`/UTF-8 + 孤儿清理默认开 | `tests/test_rule_validation.py::test_a11_locale_and_encoding_are_locked_to_utf8`、`tests/test_adapter_orphan.py::test_default_cleanup_removes_an_orphaned_target_folder`、`::test_orphan_cleanup_false_leaves_the_library_and_the_log_untouched` | 编码锁定 + 破坏性动作可控 |
| **B10** | 建链判据 = 每库账本（库/源/目标/体积全等） | `tests/test_works_ledger.py` 全 18 条（`::test_ledger_is_scoped_by_library_source_and_target`、`::test_skip_dict_becomes_the_only_judgement`、`::test_default_path_still_reads_the_global_log`、`::test_library_wide_force_is_refused`…）、`tests/test_library_log_isolation.py::test_the_second_library_is_not_fooled_by_the_first_librarys_processed_mark`、`tests/test_adapter_link.py::test_second_run_with_same_sizes_is_skipped`、`tests/test_hardlink_native.py::test_the_native_implementation_when_already_logged` | 换库/换目标不会误跳过；`skip=None` 仍走全局日志 |
| **B11** | 全局串行执行（单进程单 worker） | `tests/test_rule_validation.py::test_a12_single_process_single_worker`、`tests/test_queue_serialization.py` 全 8 条 | 同一时刻只有一个任务触碰文件 |
| **B12** | 待办看板只记录、不阻断 | `tests/test_todos.py::test_record_tolerates_missing_and_broken_keys`、`::test_count_without_usable_samples_still_records_one_entry`、`::test_todo_rows_are_not_chained_to_task_history`、`::test_no_per_kind_cap_so_one_full_scan_records_every_event` | 内部异常不外抛、不缺记 |
| **B13** | `permission_mode` 默认关闭、只动目标侧 | `tests/test_permission_mode.py` 全 9 条（`::test_default_off_is_byte_identical_to_the_old_behaviour`、`::test_enabled_fixes_the_whole_target_tree`、`::test_enabled_also_fixes_folders_that_are_skipped`、`::test_symlinks_are_never_followed`、`::test_executor_reads_the_library_flag_and_logs_the_side_effect`…） | 默认路径一个 `chmod` 都不做 |

## 4. `BASELINE.md` §5 已知限制 L1–L9

| # | 限制 | 测试 | 结论 |
|---|---|---|---|
| **L1** | 日志"读-改-写整份"，并发会互相覆盖 | `tests/test_queue_serialization.py`（8 条）、`tests/test_rule_validation.py::test_a12_single_process_single_worker` | **不修**；由 B11 串行规避 |
| **L2** | 名字含 `" - "` 的日志行被静默忽略 | `tests/test_idempotency.py::test_folder_name_containing_dash_no_longer_reports_spurious_failures`、`::test_size_change_in_a_dashed_folder_never_dedupes_the_record`、`tests/test_hardlink_native.py::test_last_failures_reports_the_full_target_path_for_dashed_folders`、`tests/test_adapter_link.py::test_existing_target_hardlink_is_idempotent_not_a_failure` | 应用侧改判 `already_linked`，文本切分不动 |
| **L3** | `legacy` 把 `None` 写进文件名 | `tests/test_bangumi_episode_rules.py::test_legacy_preset_keeps_the_old_loose_behaviour`、`tests/test_processor_engine.py::test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none`、`::test_an_unresolved_episode_keeps_the_original_name` | 既有命名；`default` 已规避 |
| **L4** | `os.rename` 平台语义会覆盖 | `tests/test_rename_collisions.py` 全 5 条、`tests/test_processor_engine.py::test_a_within_run_collision_keeps_both_files_with_a_suffix`、`::test_an_existing_target_collision_makes_the_smaller_newcomer_take_a_suffix`、`::test_renaming_a_file_to_its_own_name_is_not_a_collision`、`tests/test_adapter_link.py::test_existing_target_file_fails_without_overwriting` | 引擎按「防丢」定名，**不覆盖** |
| **L5** | 季文件夹 `fullmatch` 过严 | `tests/test_processor_engine.py::test_accept_dirs_use_fullmatch_not_search` | 既有识别规则 |
| **L6** | 季文件夹内嵌套子目录不重命名 | `tests/test_processor_engine.py::test_subdir_scope_requires_a_direct_child_and_a_whitelisted_lowercase_extension`、`tests/test_extras_classification.py::test_nested_extras_folder_is_never_touched` | 既有行为 |
| **L7** | 白名单只 `.mkv/.mp4/.ass` 且大小写敏感 | `tests/test_processor_engine.py::test_extensions_are_matched_case_sensitively`、`tests/test_rename_extensions.py`（5 条） | 两套白名单不同属既有设计 |
| **L8/L9** | 体积判定是"事后"的、等体积追加不可见 | `tests/test_idempotency.py::test_size_change_removes_the_old_record_and_keeps_one_line`、`::test_empty_folder_stays_skipped_forever`、`::test_second_run_is_a_pure_noop` | 既有幂等规则，不修 |

## 5. `ARCHITECTURE.md` §2 / §5 / §6 模块契约

| 契约点 | 测试 | 结论 |
|---|---|---|
| §1 运行依赖（含 `PyYAML`）与 UTF-8 锁定 | `tests/test_rule_validation.py::test_a11_locale_and_encoding_are_locked_to_utf8`、`::test_b1_no_original_script_dir_is_mounted_and_no_copy_ships_in_the_image` | 直接读 Dockerfile/compose 文本断言 |
| §2 单一共同父挂载 / 三态预检 / 探针零残留 | `tests/test_paths.py`（探针与挂载 13 条 + 快照核对 3 条 + 回调路径映射 15 条 + 权限预检 7 条）、`tests/test_hardlink_precheck.py` 全 14 条、`tests/test_no_source_mutation.py::test_validate_probe_never_writes_into_the_source_even_when_it_cannot_link` | `same_filesystem` 三态、`null` 不猜 |
| §2 建库/改库权限预检（应用身份实测） | `tests/test_paths.py::test_probe_media_access_rejects_a_missing_source`、`::test_probe_media_access_accepts_a_normal_library_without_leaving_probe_files`、`::test_probe_media_access_checks_the_nearest_existing_ancestor_when_the_target_is_missing`、`::test_probe_media_access_reports_an_unreadable_source`、`::test_probe_media_access_reports_an_unwritable_target`、`::test_probe_media_access_reports_a_target_that_is_a_file`、`::test_probe_media_access_warns_when_a_sampled_source_file_is_unreadable`、`tests/test_api_smoke.py::test_creating_a_library_with_a_missing_source_reports_the_running_identity`、`::test_library_save_is_blocked_by_the_permission_precheck_and_does_not_persist`、`::test_validate_reports_the_permission_precheck` | 保存即拦（400 带 uid/gid）、目标目录真写探针、源目录零写入 |
| §2 路径映射只用于展示（含键补斜杠） | `tests/test_paths.py::test_to_host_tolerates_a_map_key_without_leading_slash`、`::test_to_host_maps_and_uses_the_longest_prefix`、`::test_to_host_does_not_let_downloads_steal_downloads2`、`tests/test_rule_validation.py::test_b9_default_host_path_map_is_the_empty_identity_mapping`、`::test_b9_identity_path_map_cannot_be_overridden_by_the_image_or_compose` | 归一化必须对所有分支生效；`downloader_path_map` 是**功能项**、不是这张展示表，见下一行 |
| §5.2 / §6.1 `downloader_path_map`（回调路径换算） | `tests/test_paths.py::test_parse_downloader_map_reads_pairs_and_ignores_blank_and_comment_lines`、`::test_parse_downloader_map_rejects_a_line_without_the_arrow`、`::test_parse_downloader_map_rejects_an_empty_side`、`::test_parse_downloader_map_rejects_a_relative_side`、`::test_parse_downloader_map_rejects_a_duplicate_source_prefix`、`::test_parse_downloader_map_rejects_the_root_as_a_source_prefix`、`::test_parse_downloader_map_returns_an_empty_list_for_empty_input`、`::test_translate_downloader_path_is_a_no_op_without_a_mapping`、`::test_translate_downloader_path_is_a_no_op_when_no_prefix_matches`、`::test_translate_downloader_path_keeps_an_empty_path_empty`、`::test_translate_downloader_path_rewrites_a_matching_prefix`、`::test_translate_downloader_path_prefers_the_longest_prefix`、`::test_translate_downloader_path_does_not_let_downloads_steal_downloads2`、`::test_translate_downloader_path_refuses_a_target_that_does_not_exist`、`::test_translate_downloader_path_probes_the_translated_path_not_the_raw_one`、`tests/test_api_smoke.py::test_downloader_path_map_translates_the_content_path_before_scoping`、`::test_downloader_path_map_falls_back_to_full_when_the_translated_path_is_missing`、`::test_downloader_path_map_is_validated_on_save_and_round_trips` | 最长前缀、目标不存在→不换算+警告、保存时逐行校验并点名行号 |
| §3 数据模型（10 张表 / 账本 / 待办） | `tests/test_works_ledger.py`（18）、`tests/test_config_sync.py`（13）、`tests/test_todos.py`（27） | 表结构与语义 |
| §5.0 响应形状不变式（分页信封、超集、扁平 validate、回调参数来源） | `tests/test_api_smoke.py` 全 24 条、`tests/test_hardlink_precheck.py::test_validate_response_shape_is_flat_and_never_waits_for_combination_ok`、`::test_validate_error_and_warning_landing_is_frozen`、`::test_validate_treats_unprovable_as_a_warning_not_an_error`、`::test_system_info_mounts_are_a_dynamic_set_never_a_fixed_three`、`::test_system_info_exposes_the_same_three_state_verdict_without_probing` | 形状与落点冻结 |
| §6.1 `Settings` | `tests/test_paths.py`（映射 6 条）、`tests/test_library_logs.py::test_library_log_path_ignores_any_directory_component`、`::test_log_path_joins_without_creating_anything`、`::test_unique_log_file_avoids_existing_and_taken_names` | 派生路径与归一化 |
| §6.4 `paths` | `tests/test_paths.py` 全 71 条 | 三态、探针、快照、映射、权限预检 |
| §6.5 `script_adapter`（含日志隔离与 stdout 捕获） | `tests/test_adapter_link.py`（10）、`tests/test_adapter_orphan.py`（11）、`tests/test_adapter_scoped.py`（10）、`tests/test_file_log_isolation.py`（6）、`tests/test_rule_validation.py::test_a5_backend_takes_over_the_root_logger_with_the_frozen_format`、`::test_a6_script_prints_are_captured_not_leaked`、`::test_a7_scoped_os_is_installed_during_the_call_and_restored_after`、`::test_a7_scoped_os_is_restored_even_when_the_script_explodes`、`::test_a8_force_prunes_the_log_before_the_script_runs`、`::test_a13_seed_log_and_log_continuity_are_gone_from_the_backend` | root logger 接管、隔离过滤器、作用域恢复 |
| §6.6 `queue` | `tests/test_queue_serialization.py`（8） | 串行、取消、异常落账 |
| §6.9 `qbittorrent` | `tests/test_scanner_qbittorrent.py`（3） | 必须用模块级 `test_connection()` |
| §6.10 `todos` | `tests/test_todos.py`（27）、`tests/test_web_todo_board.py`（7） | 去重语义、不可检测种类如实告知 |
| §7 前端（免构建、hash 路由） | `tests/test_web_todo_board.py`（7） | 静态资源与 i18n 存在性 |

## 6. `tests/test_rule_validation.py` 的历史规则编号 A5–A13

这些是**测试函数名里的字面量**，不随 `BASELINE.md` 的 B 编号走。含义以本表为准：

| 历史编号 | 函数名（`tests/test_rule_validation.py`） | 归一到现行契约 |
|---|---|---|
| A5 | `test_a5_backend_takes_over_the_root_logger_with_the_frozen_format` | `ARCHITECTURE.md` §6.5 要点 2 + `BASELINE.md` §1.2（B6） |
| A6 | `test_a6_script_prints_are_captured_not_leaked` | `ARCHITECTURE.md` §6.5 要点 7 |
| A7 | `test_a7_scoped_os_is_installed_during_the_call_and_restored_after`、`test_a7_scoped_os_is_restored_even_when_the_script_explodes` | `ARCHITECTURE.md` §6.5 要点 4 |
| A8 | `test_a8_force_prunes_the_log_before_the_script_runs` | `ARCHITECTURE.md` §6.5 要点 6 + `BASELINE.md` §1.4 |
| A9 | `test_a9_no_copy_fallback_anywhere_in_the_backend`、`test_a9_copied_files_are_detected_and_reported_as_failures` | **B8** |
| A10 | `test_a10_source_tree_is_never_opened_for_writing` | **B5** |
| A10（旧） | `test_a10_is_superseded_by_b9_so_the_parent_mount_is_the_only_media_bind` | **已被 B9 取代** → `ARCHITECTURE.md` §2（单一共同父挂载） |
| A11 | `test_a11_locale_and_encoding_are_locked_to_utf8` | **B9** |
| A12 | `test_a12_single_process_single_worker` | **B11** |
| A13 | `test_a13_seed_log_and_log_continuity_are_gone_from_the_backend` | `ARCHITECTURE.md` §2（日志路径 = 容器路径）+ `BASELINE.md` §4.1 |
| b1 / b3 / b6 / b9（小写，同文件） | `test_b1_no_original_script_dir_*`、`test_b3_three_pipelines_touch_only_the_data_volume`、`test_b6_engine_logs_live_in_the_data_volume`、`test_b9_*_path_map_*` | b1→**B1**；b3→`ARCHITECTURE.md` §3（写入范围）；b6→**B6** + `BASELINE.md` §4.1；b9→`ARCHITECTURE.md` §6.1 |

---

## 7. 覆盖统计与边界

| 指标 | 数值 |
|---|---|
| 测试文件 | **29** 个 `tests/test_*.py`（另有 `conftest.py`、`mlm_helpers.py`、`processor_helpers.py`、`run_all.py`、`_minipytest.py` 等辅助文件，不含用例） |
| 测试函数 | **434** 个 `def test_*` |
| 测试类 | **3** 个 `class`（其余用例为模块级函数） |
| 本文映射条目 | **58** 行（§1:7、§2:6、§3:13、§4:8、§5:13、§6:11） |
| 契约覆盖率 | `BASELINE.md` 的 **B1–B13 全部 13 条**、**L1–L9 全部 9 条** 均有测试（L1 靠 B11 串行间接覆盖）；`ARCHITECTURE.md` §1/§2/§3/§5.0/§6.1/§6.4/§6.5/§6.6/§6.9/§6.10/§7 有直接测试，§6.2/§6.3/§6.7/§6.8 通过 `test_api_smoke.py` / `test_processor_engine.py` / `test_queue_serialization.py` 间接覆盖 |
| **不覆盖** | 跨挂载真跑、只读源目录、符号链接文件这三类**只在 POSIX/容器上被真正执行**（在无真实挂载的环境里自动 skip，而非"假装通过"）；Windows 上真跑任务不可行。原因见 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §6/§7 |

**运行方式**：开发机上 `pytest tests -q`（无 pytest 时用 `python tests/run_all.py`）；
**容器内验收用 `sh scripts/pytest-in-container.sh`** —— 镜像**不含 pytest、也不含 `tests/`**，
该脚本把测试目录拷进一次性容器、临时装 pytest 再跑。

---

## 8. 维护规则

1. **新增一个测试** → 在对应小节补一行（契约点 / 测试 / 结论），**先写契约点再写测试名**。
2. **新增一条契约** → 编号在 `BASELINE.md` 往后排，再到本文件建行；**不复用**旧编号。
3. **映射表只在这里有**。`COMPATIBILITY.md` §3.2 等位置只放指针，**不得再抄第二份**（历史上就是两处
   各自维护才漂移出错标）。
4. 本文不放运行记录、不放环境信息、不放历史结论 —— 这些去 [`EVIDENCE.md`](EVIDENCE.md)。
