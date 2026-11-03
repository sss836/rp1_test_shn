# 研发／工厂记录分区（2026-10-29）

本批为源码交付，不含打包、安装、发布或现用控制实例重启。工作树原有运动收尾、线程保护和 OTA 修改保留。本文件对应 `outputs/animal-office/factory-partition-20261029/IMPLEMENTATION_CONTRACT.md`；实际部署和现场验收另行记录。

## 行为与数据链

- 顶栏选择研发／工厂模式，选择独立于台架前缀，启动时固定本次模式、SN、实际台架和操作员快照。新窗口默认研发模式。模式切换仅影响后续请求。
- 工厂在线样机输入为自由 SN，配置及普通启动都不依赖研发样品目录或计划 UUID；研发在线仍仅允许当前部位已登记且未运行的样机。快速验证在研发在线时执行同样的样机检查；工厂允许未登记 SN。网络在线判断和监控没有伪装为离线。
- 工厂普通测试仍要求实际台架和测试名称；没有对应正式用例时使用 OTHER 并填写临时名称。快速验证的名称来自所选内置验证。SN 按文本保留大小写和前导零，输入首尾空白按原界面习惯去除。每次运行沿用独立 UUID／本地记录 ID，同 SN 复测不覆盖旧文件。
- 普通／quick 请求都传 `data_domain`、`bench_id`、`operator_id`；服务根据本次原始 `robot_id/sample_id` 固定工厂 `serial_number`。`operator_id` 是显示快照，核实提交仍绑定当前真实登录账号，后台决定权限。
- 采集只写本机 CSV 和 context；结束后复制为封存原件、生成 manifest、报告及包。业务分区保存在身份元数据中，不向测量 CSV 添加列，不改原始测量事实。
- 封存身份、可重建配置、报告、历史列表、台账 CSV 和 presence 均携带分区。历史列表支持分区、SN、台架、日期组合筛选，分页和导出使用同一条件。台账末尾增加数据分区、依据和工厂 SN，原列次序保留。
- 新记录显式模式优先（例如 A10 上的研发、B20 上的工厂）。旧客户端或历史无模式时仅按有效 `bench_id` 推导：A 开头工厂、其他研发；空值、unknown、UUID 代替编号等无台架依据的记录为 `UNCLASSIFIED`。历史读取只形成视图并重建台账，不改旧 manifest、包或原 CSV。

## 归档、审批及失败恢复

只有人工核实并明确提交才进入 outbox；仍先验证封存包和 CSV 哈希，再追加核实版本。请求通过专用 `reliability_v1` CSV 归档通道，保留原始文件本地副本。

归档／历史导入新增 `bench_id`、`serial_number`，有显式模式证据才发送 `data_domain`。历史推导的模式不伪装成显式选择，`data_domain_source` 由后台自行判断。当前模式、当前顶栏台架及核实后的实际对象文字不会覆盖原始 SN 快照。旧冻结请求保持原内容。

后台归档回执确认工厂后，HMI 不再要求研发 classification UUID；核实成功后复用 `/archives/{id}/submit` 明确送审。工厂审批使用独立记录，HMI 只查询本人归档详情核对执行 UUID、`is_owner`、审批状态和版本，不调用研发 `/submissions/{id}`。研发路径的正式归类和站点修正流程保留。

旧工厂 outbox 若停在 awaiting_classification，后台确认工厂且尚未送审时转为 needs_verification，要求人工重新核实、显式提交；不会因升级自动送审。历史 CSV 缺运行事实仍只归档查看，不伪造时长或循环数。分区待归类与正式用例上下文未归类分别表达。

上传保留冻结载荷、分块偏移、幂等键、核实修订和审批修订；丢失回执重试同一请求。哈希变化、错误所有者、版本冲突和驳回仍关闭当前操作，驳回需重新核实。账号登录和监控在线不代表已归档或已批准；正式统计门禁由后台执行。

## 文件连接

| 文件 | 本批变化 |
| --- | --- |
| `factory_hmi/data_domain.py` | 分区证据规则、原始 SN、归档字段；纯读取派生函数 |
| `desktop/app.py`、`desktop/sample_selector.py`、`desktop/quick_page.py` | 选择模式、两种启动快照、工厂自由 SN、研发目录门禁、历史筛选与分区标签 |
| `client.py`、`gateway/schemas.py`、`gateway/app.py`、`gateway/quick_run.py` | 两种请求及历史筛选／导出传递 |
| `core/service.py` | 启动身份、context、运行 snapshot、封存及本地历史查询 |
| `core/report.py`、`contracts/execution-bundle-v1.schema.json` | manifest、重建白名单、报告、历史只读派生 |
| `execution_history.py` | 组合筛选、台账 CSV 新列 |
| `sync/presence.py` | 每次运行的分区、SN、实际台架、操作员，不依赖可变连接台架 |
| `sync/reliability_platform.py` | 归档字段、工厂送审及本人回执轮询；保留封存和幂等门禁 |
| `tests/test_factory_partition.py` | 新分区场景、fake 采集、mock 归档、离屏界面及 ASGI 检查 |
| `tests/test_quick_page.py`、`tests/test_verified_csv_archive.py`、`tests/test_frozen_evidence.py` | 更新新增字段契约；研发正式场景明确选择研发，历史导入保持无采集事实约束 |

## 软件验证记录

测试仅使用临时目录、合成 CSV、fake 电机、mock 客户端；公共 `tests/conftest.py` 为每个测试配置独立 mock PLC。未启动真实 PLC/CAN 测试，也未操作现用控制进程。

系统 `python3` 未安装 pytest，本批只读复用仓库已有交付中的 Python 3.12 测试依赖：

```bash
export PYTHONPATH=hmi:hmi/dist/local-2.0.12-jammy-portable1-20261020/build-inputs/runtime:hmi/dist/local-2.0.12-jammy-portable1-20261020/build-inputs/test-runtime
export QT_QPA_PLATFORM=offscreen
export PYTHONDONTWRITEBYTECODE=1
```

2026-10-29 正式实施接续检查：分区、普通／quick、核实归档、封存、历史、核心服务与网关合计 **170 passed、11 subtests passed**（同一轮，1 条依赖弃用警告）。原 quick 场景已按完整内置周期设置 20 秒预算并取得最终通过记录。

旧 TestClient 挂起已复现并查明环境原因：沙箱中的 `socket.socketpair().send()` 返回 `PermissionError(1)`，跨线程 portal 因此无法唤醒事件循环，挂起发生在进入应用 lifespan 之前。同一测试组获准在沙箱外执行后 **17 passed**，包含原挂起用例；未删除或跳过该检查，也未修改网关控制逻辑来适配环境。完整最终 HMI 组同样在沙箱外使用 mock PLC、fake 电机与临时目录执行。

新增后端串联检查使用真实 `FactoryService` fake 采集、`ReportManager.rebuild` 和 `ReliabilityUploaderWorker`，只将 HTTP 传输适配为内存 TestClient，实际经过临时 PostgreSQL、账号登录、原始 CSV 解析、核实、送审与工厂审批。同一含路径字符且保留前导零／大小写的 SN 复测两次，分别封存并批准，原件与哈希不变，无研发执行投影。源码见 `backend/tests/test_factory_hmi_archive_integration.py`。

本地台账沿用界面明确标注的 SN／台架部分匹配，平台归档 SN 查询为区分大小写的精确匹配；各自列表和导出共用同一筛选。标识本身不因查询方式改变。显式工厂 SN `unknown` 作为正常文本保存，只有无显式模式的旧占位值才视为缺失。

本轮实际日志、命令、完整源码清单和缺口见 `outputs/animal-office/2043aa63ee384015b5415695edf111a4/DELIVERY.md`；既往日志不计入本轮通过数。

尚未执行安装生效核对、现场设备验收或长期稳定性观察；本批也未改变运动参数、轨迹或 OTA 行为。
