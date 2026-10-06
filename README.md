# 基本养老服务资格网

面向县民政部门的基本养老服务资格网领域内核：保存老人授权、风险与需求评估、
服务目录与政策时效、县乡村责任、设施容量、护理员资质、预约与真实履约，
并保证每一次探访只在**服务发生当时**有效的政策与授权范围内生成可追溯记录。

系统采用事件溯源：所有业务事实只追加、不改写；进程重启后重放事件日志即可恢复
探访期限、升级状态、停业窗口与改派结果。

## 目录

- `contracts/domain.schema.json`：领域事件信封与已登记的事件/聚合类型。
- `data/sample.json`：中文联调样例。
- `src/elderly_basic_services/contracts.py`：不依赖第三方包的交换层校验器。
- `src/elderly_basic_services/state.py`：纯函数事件重放（状态归约）。
- `src/elderly_basic_services/store.py`：JSONL 追加型事件存储（重启可恢复）。
- `src/elderly_basic_services/service.py`：命令侧，全部业务规则在写事件前校验。
- `src/elderly_basic_services/readmodel.py`：县乡村层级视图与按授权的可见性。
- `tests/test_contracts.py`、`tests/test_network.py`：契约与业务场景检查。

## 核心规则与实现位置

| 业务要求 | 规则落地 |
| --- | --- |
| 探访只在当时有效的政策与授权范围内生成 | 排班、派单、履约三处都按“服务发生时刻”重算政策版本、资格区间、授权区间并写入 `snapshots`；事后政策换版/撤回/迁居不改写历史事件 |
| 授权方式（本人、书面代办、口头见证、紧急推定） | `grant_consent`：书面代办必须记录委托人，口头授权必须有见证人；授权可限定服务事项、被授权人与有效期 |
| 家属代办 | 非紧急代下单须有指向该代办人的 `administration` 授权，否则 `agency_required` |
| 紧急先上门、先转介，后补材料 | `trigger_emergency` 凭 `emergency_life_safety` 法定依据先行，并记录最小知情响应人；`complete_paperwork` 后补有效授权或主管核签，二者必居其一 |
| 隐私边界不绕过 | 非紧急路径无授权一律拒绝；紧急响应人视图只看到开放转介的最小字段；代理人只看到被授权事项；护理员只看到派给自己的单 |
| 跨村居住 | 行政单元用 `县/乡/村` 编码；`relocate` 记录生效日，改派优先现住村、再同县；历史快照保留原住地 |
| 撤回同意 / 迁居只影响未来 | 撤回与迁居都是带时间点的新事件；已履约事件物理保留，不可取消、不可覆盖，只能开 `correction_of` 更正记录 |
| 助餐点停业只改派受影响日期 | `suspend_facility` → `reassign_suspended`：仅命中停业区间的排班改派；其他日期、其他村计划、护理床位继续运行；无处承接时转上门 |
| 设施容量与护理员资质 | 按日容量校验 `capacity_exceeded`；资质带有效期，过期即 `qualification_required` |
| 报送幂等、晚到原样返回 | `submit_record` 以稳定业务键去重，忽略 `submitted_at` 等易变字段，重复/晚到返回既有受理回执 |
| 内容矛盾不静默合并 | 同键内容不一致 → `REVIEW_OPENED` 交指定复核人，既有结果保持不变，复核结论为 keep_existing/replace/reject_new |
| 同一人多项目重复登记 | 证件号唯一建档，重复建档 `duplicate_identity` |
| 重启保留期限与升级状态 | JSONL 追加日志重放：探访期限、开放转介、风险升级、停业窗口、撤回状态全部恢复 |
| 主管按县乡村查看 | `hierarchy_view`：覆盖人数、在册资格、真实履约量（更正不重复计数）、探访/节奏/紧急缺口、责任分工、设施当日容量 |

## 使用示例

```python
from datetime import date, datetime
from elderly_basic_services import EligibilityNetwork, EventStore, Viewer
from elderly_basic_services import hierarchy_view
from elderly_basic_services.readmodel import elder_view

net = EligibilityNetwork(EventStore("data/events.jsonl"))
net.publish_policy("meal_dining", "老年助餐", "basic_living", date(2026, 1, 1),
                   cadence_days=7, required_for_categories=("living_alone",))
net.register_elder("e-001", "王兰", "甲县/东乡/河东村", categories=("living_alone",))
net.grant_consent("e-001", "g-1", "service_delivery", "self",
                  service_codes=("meal_dining",), valid_from=date(2026, 1, 1))
net.assess_need("e-001", "a-1", "assessor-li", service_codes=("meal_dining",),
                risk_level="medium", visit_due_by=date(2026, 10, 31))
net.grant_entitlement("e-001", "meal_dining", "ent-1", date(2026, 10, 1), assessment_id="a-1")

view = hierarchy_view(net.state, Viewer("boss", "supervisor", scope="甲县"), today=date(2026, 10, 6))
```

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
