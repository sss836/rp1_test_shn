# 整机工厂测试接入与机器人总览

> 本文保留 2026-11-05 独立桥接阶段的范围与验收记录。2026-11-06 原生登录、研发／工厂分区及后续交付状态以 [原生平台接入说明](NATIVE_MONITOR_PLATFORM_INTEGRATION_20261106.md) 为准。

## 范围与来源

本轮按用户确认的机器人卡片效果实施：保留 RP1 可靠性大屏原有品牌、浅蓝网格、机器人展示区与用例翻页布局，增加整机机器人总览。卡片显示在线／离线、机器人身份、工位，以及具体做过的工厂测试项目。卡片不展示电机温度、观测时长或据此推算 MTBF。

整机上位机已下载在 `/home/shn/桌面/rp1-monitor-app`，来源为 `sss836/rp1-monitor-app` 的 `main@4b325a518b94859437cbed9bff9798b1db12f87c`，原版本 `0.4.0`。本地适配分支为 `codex/factory-platform-bridge-20261105`。平台改动分支为 `codex/factory-robots-20261105`，基于 `35cd7e70312f760a4c222475a4ea67d784c4ba71`。不修改整机 Qt、机器人连接／控制、采集策略、原有 SQLite／PostgreSQL 保存规则；不安装或重启正在工作的 PLC 上位机。

## 为什么需要单独适配

`rp1-monitor-app/src/rp1_monitor/factory_result_summary.py` 的 `export_result_summary()` 导出固定 17 列工厂汇总。记录编号由 SN、项目／子项和证据来源生成，重复导出保留编号；重新编辑可改变同一编号对应的最终填写结果。文件内包含 `SYS-001～SYS-012` 父项目和基础自检、行走／跑步等子项，也可能包含尚未测试的项目。

平台原有 `/api/v1/archives` 处理原始数值时序 CSV，并生成曲线查询文件。17 列汇总中的“实际结果”“通过标准”等为文本，不是时序采样。此次新增 `reliability_v1` 专用工厂结果归档通道，不能将汇总文件当作原始曲线输入，也不能通过旧 ingestion、通用附件或执行包传入。

上位机内这组 `SYS-xxx` 项目属于 **FACTORY**。它们不与研发 `SYS-REL-xxx` 用例自动等同，不从记录时长推导动作运行时长，也不写研发执行、暴露时长或 MTBF。

## 文件级实现

| 文件 | 新增／修改及连接行为 |
| --- | --- |
| `frontend/src/pages/WholeMachineHome.tsx`、`wholeMachineHome.css` | 新增总览／聚焦切换、静态机器人小图、在线筛选和身份搜索；保留原 `HomePage` 的机器人舞台及机械翻页。按 source+robot 独立导航，不按重复 robot_id 合并。 |
| `frontend/src/components/RobotFactoryResults.tsx`、`robotFactoryResults.css` | 新增工厂汇总文件核实提交、按来源／SN 查询、17 列原值、原件下载和独立审核；回执不确定时保留冻结文件与幂等键。 |
| `frontend/src/robotFactoryApi.ts`、`App.tsx`、`navigation.ts`、`pages/FactoryPage.tsx` | 新增专用接口与 source 深链接；指定机器人时只展示对应整机汇总，普通查看账号不挂载受限 PLC 监控。原 PLC `FactoryMonitor` 和模块 `HomePage` 未修改。 |
| `backend/app/api/routes/factory_robots.py`、`schemas/factory_robots.py` | 新增 `reliability_v1` 工厂专用入口、4 MiB 请求体、核实／结束声明、账号角色与参数门禁；上传返回轻量回执，逐行详情另查。 |
| `backend/app/services/factory_robot_results.py` | `parse_result_csv()` 校验固定列、单一 SN／机型、SYS 工厂目录和父子项、时间、模拟来源；`FactoryRobotResultStorage` 原子发布只读文件，并在下载／批准前复核哈希。 |
| `backend/app/repositories/factory_robots.py` | `robots()` 最多读取本页 200 个机器人，数据库聚合当前已批准的项目，只回传轻量字段；查询开始前设置 3 秒 SQL 预算。 |
| `database/sql/060_factory_robot_result_archives.sql`、`database/alembic/versions/20261105_0060_factory_robot_result_archives.py` | 新增来源绑定、当前在线投影、不可变汇总／逐行版本、审核及幂等回执；写操作使用受限函数、行锁／事务锁和 RLS。 |
| `backend/app/core/auth.py`、`main.py` | 注册新路由；工厂审核员只新增对应 UUID 归档的 review 写入口，不能上传、发心跳或访问研发业务。 |
| 整机仓库 `src/rp1_monitor/factory_bridge.py`、`pyproject.toml` | 新增独立 `PlatformClient`、`MonitorClient`、`Outbox` 和 `rp1-factory-bridge` 命令，不导入 Qt／station／relay。使用原只读 API，明确提交既有结果文件。 |
| `scripts/test-robot-factory-bridge.py`、`qa_robot_factory_bridge.py`、`qa_robot_factory_tls.py` | 一次性 PostgreSQL／文件卷验收，真实 HTTP 加原整机 `FactoryStore` 导出，并验证可信 CA 与 TLS 慢响应；不读取现场 `.env`，结束清理隔离资源。 |

## 数据流与人员操作

1. 仍在原整机上位机中完成记录、人工填写与“一键导出 CSV”。原始证据和导出文件继续保存在工位。
2. 人员确认本次测试已结束，逐项核实 SN、上位机编号、结果及模拟标记后，在平台工厂页面显式提交文件，或运行独立桥接程序的 `submit-results` 命令。程序不扫描目录自动提交未核实文件。
3. 专用入口验证固定列、身份一致性、文件大小及 SHA256，保存原文件和不可变记录版本，产生待审核的工厂汇总归档。失败不生成正式结果，重试使用同一幂等键。
4. 执行员、管理员或工厂审核员查看原文件／逐行结果后批准或驳回。允许按现有权限自审，但核实上传与审核是两个明确操作；登录批准不代表数据批准。
5. 驳回后通过新提交追加修订；原文件、旧记录值、提交人和审核理由继续保留。同一 SN 的不同记录编号是不同尝试，不能合并成一条测试。
6. 机器人总览独立接收在线快照。掉线后保留历史项目，不把掉线等同于测试停止或完成。网页请求失败时不继续显示绿色在线。只展示当前版本已批准的父项目；未测试、不适用和子项不重复计数。更正版本尚待审时，旧版本不继续作为当前已批准项目展示，但旧文件及审核仍可查询。

汇总版本获批准，只说明这份工厂结果汇总通过审核。它没有凭空补齐平台正式用例版本、执行上下文、采样完整性或原始曲线证据。正式可靠性统计仍按既有资格与审批流程处理；待审核、驳回及模拟结果不作为正式统计输入。

## 接口约定

这些接口全部使用当前平台账号会话 Cookie；写请求必须有 CSRF。独立桥接程序不使用旧 ingestion Bearer，不保存平台密码、会话或机器人凭据。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/factory/robots` | 获取已登记来源的机器人卡片及已批准项目；模拟来源明确标记 |
| `POST /api/v1/factory/robots/presence` | 来源绑定账号后的完整在线快照；45 秒无新上报自动离线 |
| `POST /api/v1/factory/robot-result-archives` | 核实后显式提交固定 17 列工厂 CSV，原始请求体上限 4 MiB |
| `GET /api/v1/factory/robot-result-archives` | 按来源／SN 等查看工厂汇总台账 |
| `GET /api/v1/factory/robot-result-archives/{id}` | 查看文件身份、逐行结果、版本和审核信息 |
| `GET /api/v1/factory/robot-result-archives/{id}/file` | 经权限检查下载哈希核对后的原文件 |
| `POST /api/v1/factory/robot-result-archives/{id}/review` | 按预期版本批准／驳回，保留理由与审核人 |

上传元信息含 `source_id`、`robot_id`、`source_kind=real|simulated`、`filename`、`size_bytes`、`sha256`、`verified=true`、`completed=true`、`reason`；必须带 `Idempotency-Key`。`source_id` 必须等于 CSV 的“上位机编号”，不是任意临时标签。SN 保留大小写和前导零，不能从网络地址或机器人名称猜测。

## 长期运行与存储

在线快照只保存机器人身份和状态，不把每次轮询变成长时监控采样，也不在审计表追加逐次心跳。来源首次绑定和结果、审核继续保留审计。汇总归档只上传已核实的关键结果及原 CSV，原始文件位于平台配置的外部 `ARCHIVE_ROOT/factory-robot-results/`，PostgreSQL 保存身份、哈希、结果版本、提交及审核记录。桥接上传失败时保留冻结任务以重试；本机原文件不因平台收到回执而被删除。

桥接待传载荷预算为 32 MiB／64 项，容量检查与入队同在 SQLite `BEGIN IMMEDIATE` 事务中，满后拒绝新任务而保留已有任务。最多保留 512 个已完成的轻量回执；这不是整个 SQLite 文件或进程 RSS 的承诺上限。原始结果上传最大 4 MiB／1000 行，适用于关键汇总，不能把几千小时逐点波形装入该通道。

重试与重新提交分开：网络回执丢失继续使用同一 pending 任务与幂等键；已经收到回执后，人员再次明确提交生成新请求身份。服务器对同文件的待审／已批准归档去重，驳回后可以创建新的待审归档及逐行版本，不能为了重提删除 outbox 或修改原始事实。

服务端来源绑定与幂等处理在同一数据库事务中加锁。两次同键相同请求只产生一个归档，改用同键发送不同内容冲突回滚。审核必须匹配当前状态 revision，只有一次决策能成功；`revision` 是审核状态的并发版本，不是原 CSV 被编辑的次数，逐条结果的 `row.version` 才标识追加的记录版本。文件与数据库之间没有分布式事务：文件通过原子 link＋fsync 发布，数据库随后提交；异常回滚可能保留未被数据库引用的文件，不能自动把它算作已收或批准。重试依靠原幂等键和哈希复核恢复；人工恢复须同时核对数据库索引和文件，不删除证据来消除错误。

单次网络请求包含 socket 总截止监视器，慢速响应到期会关闭实际连接，并取消、回收截止计时线程；没有超时后留在后台继续读的工作线程。平台使用 HTTPS 及可信 CA，源监控仅访问 `127.0.0.1` 的只读接口。域名解析仍受操作系统 resolver 行为约束，不能声称具有独立可强制中断的 DNS 预算。

在线桥接使用现有人员会话，默认有效期 8 小时，以平台配置为准；401 后退出，需要人员重新登录。该候选不承诺无人值守几千小时持续上报在线状态。会话到期不停止原 Qt、机器人或本地记录；网络和身份状态也不作为设备安全停止反馈。

本轮不改变原整机监控数据库的长期保留行为。它仍有既有 PostgreSQL 监控记录，不能把“平台只接关键汇总”说成“上位机只存关键汇总”，也不能据此承诺几千小时监控数据已有自动清理或分区。

## 使用与后续合并

整机侧配置与命令见本地整机仓库 `docs/FACTORY_PLATFORM_BRIDGE.md`。源监控 API 没有 SN，首次配置必须人工提供 robot_id、SN 和明确的 24轴／28轴机型；不能用界面默认机型猜测。平台密码通过终端隐藏输入，Cookie／CSRF 仅在桥接进程内存中；原 viewer token 只用于本机只读请求。

当前来源首次绑定提交账号，**没有**管理员转移／解绑入口。账号停用或失去提交权限后不会继续显示在线，也不能用别的账号抢占来源；换班接管需要后续实现受审计的来源授权迁移。同一 source+robot 不能静默改成另一 SN／机型，不能删绑定、改 source 编号或清历史绕过冲突。此项是当前候选的运行限制。

本轮按用户要求只保留本地独立分支，不上传 GitHub，不创建 PR，不合并其他正在测试的功能，也不替换现有运行实例。后续合并应同时检查两仓库的 API 契约，特别是平台 `App/navigation/auth/main`、整机 `README/pyproject` 和迁移链；若其他功能占用 0060，应重新编排迁移 revision/down_revision，不能产生两条未处理的 Alembic head。正式更新前先核对当前部署配置中的固定镜像与持久卷，再部署匹配的迁移／API／前端，最后配置实际工位并现场联验。

验证命令：

```bash
cd /home/shn/桌面/rp1_test_shn-e56cabd
python3 scripts/test-robot-factory-bridge.py --monitor-repo /home/shn/桌面/rp1-monitor-app
cd frontend
npm run check
```

第一条使用隔离 Docker 数据库／文件卷和 `rp1-archive-qa:local` 工具镜像，运行相关后端检查与真实 HTTP 桥接验收，不连接现场设备。

## 验证与运行状态

2026-11-05 实际通过：

- 最终前端 `npm run check`：50 个测试文件、410 项测试，ESLint、TypeScript 和生产构建通过。构建保留既有大图表 chunk 大小提示，不影响构建成功。
- 主 agent 的全新隔离数据库验收：相关后端 77 项通过，包含 CSV 校验、Cookie／CSRF、角色、并发幂等、不可变性、模拟数据隔离、100 次心跳不追加审计，以及非本页长证据不进入卡片返回。另有实施 agent 的 51 项隔离回归和机型冲突定向检查作为交叉证据，不将重复检查累加成不同功能。
- 整机桥接 26 项＋原结果导出 2 项，共 28 项通过，无 skip；实际执行 HTTP 响应头／响应体慢滴流故障、多地址连接预算和任务重提回归。
- 最终跨仓真实 TCP：原 `FactoryStore` 导出 31 行，登录、原 bytes 上传、独立批准、项目卡片、原记录更正追加版本、同原件驳回后再次明确提交、离线及原文件不变均通过；确认未产生研发执行。TLS 探针验证不可信证书拒绝、配置可信 CA 后成功、慢响应到期中断且无计时线程遗留。
- 临时容器中的 Ruff 检查通过；两仓库 `git diff --check` 通过。临时数据库与文件卷已清理。

可核对日志在忽略目录 `outputs/whole-robot-factory-20261105/`，其中 `frontend-check.log`、`platform-bridge-qa.log`、`monitor-bridge-final.log`、`bridge-final-tcp-tls.log` 对应上述实际命令。源代码与可移植补丁在本地分支及 `hmi/dist/whole-robot-factory-bridge-20261105/` 交付；构建／源码清单和 SHA256 以该目录为准。

本轮是本地源码候选，未执行现场数据库迁移、未替换 API／网页镜像、未安装或重启 Qt／PLC 上位机、未配置真实工位绑定、未启动真实桥接、未推送／合并／发布。现场平台迁移仍为 `20261103_0059`，API 仍为此前 `3.0.21-standardcsv2`，新页面尚未在该运行实例生效。浏览器访问此前被管理策略拒绝，没有换工具绕过，也未进行真实浏览器像素截图核对。隔离软件测试不代表真实设备或几千小时稳定性验收；这些现场事项保留到后续合并与部署阶段。
