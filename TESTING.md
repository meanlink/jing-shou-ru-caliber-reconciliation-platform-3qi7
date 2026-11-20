# 经营周报脱敏与分发 — 测试说明

## 环境事实

- 本沙箱**没有 Docker、pip、MySQL、Redis**，无法启动 `docker compose` 五服务全栈，也无法安装 FastAPI/SQLAlchemy。
- 因此本轮验证采用**纯 Python 标准库单元/集成测试**：核心脱敏引擎零第三方依赖；服务层测试在进程内注入最小 FastAPI/SQLAlchemy 桩，用内存假 DB 驱动完整发布编排。
- 所有 Web/ORM 接线文件均通过 `python3 -m py_compile` 与 `node --check` 语法校验；真实容器内的端到端流程、权限与截图未在本环境执行，不写成已通过。

## 运行方式

```bash
# 仓库内（本沙箱实际使用）
python3 -m unittest discover -s test -v

# 规范位置 test-artifacts/gb-1018/tests 同样可运行，测试会自动定位 backend/
python3 -m unittest discover -s test-artifacts/gb-1018/tests -v
```

结果：20 个用例全部通过（`Ran 20 tests ... OK`）。

## 用例与业务规则映射

### `test_disclosure.py`（引擎层，14 例）
| 用例 | 覆盖规则 |
|---|---|
| IntegerSpanTests ×3 | 整数高斯消元、差值攻击（含负系数）线性相关判定 |
| test_threshold_boundary | 访问恰好 100 不遮、99 主遮挡；被遮行不进发布内容 |
| test_internal_total_anti_complement | 合计行反推保护：两行中一行被遮时合计必须连带遮挡 |
| test_all_suppressed_is_blocked | 全部遮挡 → blocked，无任何可下载内容 |
| test_difference_attack_between_two_reports | 同受众两份周报差值拼合 → 二次遮挡；不同受众不关联 |
| test_other_audience_is_not_linked | 受众隔离 |
| CrossFilterAtomProtectionTests ×2 | 受保护渠道日不是报告行时，经聚合反推仍拦截；无保护原子时不误伤 |
| test_republish_already_visible_value_is_not_overblocked | 同受众再发已可见值不扩展披露，照发（避免过度拦截） |
| test_mixed_detail_and_aggregate_blocks_difference_attack | 明细+聚合同报告差值攻击，聚合与合计连带遮挡、高访问明细照发 |
| test_held_then_recheck_after_first_release | 并发 held，先发后重审；重审确定性（恢复原次发布） |
| test_retry_same_snapshot_no_new_disclosure | 同输入重试可见向量一致；台账重复写入不新增披露方向 |

### `test_weekly_service.py`（服务编排层，5 例）
| 用例 | 覆盖规则 |
|---|---|
| test_first_publish_creates_artifact_and_snapshot | 发布写 CSV（被遮金额为 `***`）、SHA256、披露快照 |
| test_retry_same_window_is_idempotent | 同窗口重试复用同一记录，attempts+1，不新增 Delivery/披露 |
| test_concurrent_second_report_held_when_union_leaks_protected_atom | 合起来会泄露 → 后发者 held（含可反推原子清单）；下窗口重审并遮挡 |
| test_concurrent_no_leak_both_publish | 合起来不泄露时两份都正常发布，避免过度拦截 |
| test_blocked_report_has_no_download | blocked 无下载文件 |
| test_different_audiences_do_not_block_each_other | 不同受众并发互不影响 |

## 关键设计不变量

1. **下载只含发布内容**：`_render_csv` 对被遮行写 `***`，拦截原因不进 CSV，只在 API/页面展示。
2. **已能下载的都算入台账**：仅 `status=published` 的快照进入 `audience_snapshots`；held/blocked 不计入。
3. **重试不新增披露**：`idempotency_key = wr-{report_id}-slot-{时间窗}`，命中即复用记录与文件。
4. **并发判定基于真实泄露**：只有并集空间新推出某个访问<100 的渠道日原子时才 held，否则照发。
5. **试看不落库**：`/weekly/preview` 不写 Delivery、不写文件、不改台账。

## 待真实容器补验的项

- MySQL 8 下 `weekly_reports`/`weekly_deliveries` 建表与 JSON 列持久化（迁移走 `Base.metadata.create_all`）。
- Redis 锁 `weekly:publish:lock` 在多 worker 下的互斥。
- 前端三角色按钮/路由权限与实际下载链路（Nginx → FastAPI FileResponse）。
- seed 演示批次在页面上的遮挡、拦截原因、held/重审全流程截图。
