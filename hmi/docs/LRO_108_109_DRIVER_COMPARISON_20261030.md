# LRO 108 / 109 驱动对比与上位机改动清单

日期：2026-10-30。范围：上游固定提交与当前工作区源码的静态对比、数值核算。本轮只写对比记录和取证产物，没有修改应用驱动、构建安装包、更新固件、安装或操作真实 CAN/PLC。

## 对比基准与版本名称

| 对象 | 核对结果 |
| --- | --- |
| roboparty_deploy 默认 main | [1aa66bd2888879c62b375612dec8cfdc8925331d](https://github.com/Roboparty/roboparty_deploy/commit/1aa66bd2888879c62b375612dec8cfdc8925331d)，提交时间 2026-10-30 02:32:09 UTC |
| 部署仓库实际锁定的电机子模块 | src/motors → [roboparty_motors 2ee21b2f88f35bd253801d7a8142477f94e2624d](https://github.com/Roboparty/roboparty_motors/tree/2ee21b2f88f35bd253801d7a8142477f94e2624d)，2026-10-27 07:55:37 UTC；检查时也是驱动默认分支最新提交 |
| 上游 SDK 包版本 | package.xml 为 2.3.1；当前 hmi/motors/package.xml 为 2.2.1。这是驱动库版本，不是电机固件版本 |
| 上游 109 固件证据 | [4db442c](https://github.com/Roboparty/roboparty_motors/commit/4db442ca53255c0a53f41e1a2968c6df68acb450) 新增 L5N_RZT2L_CAN_CDH2_V5.0.0.109_202610212147_Release.lsepbin |
| 上游历史 108 证据 | [31630d0](https://github.com/Roboparty/roboparty_motors/commit/31630d0089d14a3cff72cc480d81f3bb7245af05) 新增 L5N_RZT2L_CFD_CD_V5.0.0.108_20260813_TEST.lsepbin |
| 本地比较对象 | hmi/motors、factory_hmi 后端/模型/界面、脚本与打包校验；Git 基线 d04ef76，包含已有未提交改动 |

108/109 在此为用户及仓库使用的固件简称，不能改写为 1.0.8/1.0.9。上游新故障定义明确标注协议 V1.03，但仓库没有提供足够证据证明“所有 108 必然用旧布局、所有 109 必然用新布局”，也未证明不同硬件后缀可互刷。以下以“当前源码行为”和“109 随附上游源码行为”准确区分。没有下载或烧录固件二进制。

核心协议改动来自 [2c33315：V1.03 故障解码](https://github.com/Roboparty/roboparty_motors/commit/2c333152f0b6fe9047039bdd0ea9c4b36d2c2409) 和 [024ede9：LRO 范围及协议处理](https://github.com/Roboparty/roboparty_motors/commit/024ede98e89a71ab27421b3013bef071fb324ca1)，不是仅更换固件文件。

## 1. 故障位宽、含义和使能状态

当前 canfd_rx_cbk 使用 error_id = Byte0 & 0x1F；上游改为 Byte0 & 0x07，明确 bit3 保留、bit4 是新固件的使能状态。帧类型仍由高三位决定。

| type-1 的 Byte0 | 当前代码的 error_id | 上游的 error_id | 含义 |
| --- | --- | --- | --- |
| 0x20 | 0 | 0 | 无故障 |
| 0x28 | 8 | 0 | 保留位不应当成故障 |
| 0x30 | 16 | 0 | 新协议的使能位不应当成故障 |
| 0x27 | 7 | 7 | 低三位确实有 7，改掩码不会消失 |
| 0x37 | 23 | 7 | 故障 7 与使能状态应拆开 |

新枚举还增加 0x05“缺相或电流采样偏置”，0x06 从旧命名 BRAKE_OVERVOLT 改为过压，0x07 从 DRV_ERROR 改为 OTHER_ERROR，0x01 明确包含电机/MOS 过温。

必需改动：lro_motor_driver.cpp/.hpp 按已确认协议解析，并保留原始 Byte0、帧类型及可用的使能位；desktop/app.py 两处固定 DRV_ERROR 文案改为协议对应的报警名称。0x07 应保留，不能通过改名或掩码将其消除。详细报警需另行读取；头文件仅写“Query code 36”，没有明确进制、长度与字段布局，实现前需核实协议，不能直接猜成 0x36。

补强建议：新增可空的 drive_enabled 观测，区分“软件已发使能指令”和“驱动新帧确认使能”。controller.enable_motors 已有新反馈确认，可以在确认过该能力的新协议中进一步校验实际状态；不能将该位强行应用到未确认的 108。

用户确认此前读取均未使能，因此 bit4 的掩码差异不能直接解释那次真实低三位 0x07。此前“DRV_ERROR”的解释仅对应旧 SDK 命名，应随着协议识别修正。

## 2. 速度、力矩和电流范围变化

上游 lro_limit_param 的字段由 PosMax/SpdMax/TauMax/KpMax/KdMax，增加独立 CurMax。

| 型号 | 当前 SpdMax → 上游，rad/s | 当前 TauMax → 上游，N·m | 上游新增 CurMax，A |
| --- | --- | --- | --- |
| 5550 | 21 → 20 | 36 → 30 | 18 |
| 6562 | 21.89 → 40 | 35.3 → 40 | 32 |
| 8462 | 16.13 → 35 | 56.5 → 65 | 35 |
| 10062 | 14.24 → 16 | 141.7 → 165 | 90 |

PosMax 仍为 12.5rad，Kp/Kd 编码上界仍为 500/50。MIT 的字段位数、拼包布局和批量扩展 ID 0x8080 没有发生对应修改，但字段所代表的物理量范围改变，因此同一二进制值代表的速度/力矩不同。

必需改动：将参数做成按节点/型号/已确认协议选择的编码配置，发送与接收使用同一组已核实范围，不能只改反馈解码。若还需兼容现有 108，不应全局替换 lro_limit_param；可显式配置已批准的 profile，并用版本及参数读回核对。版本未知时保留原始诊断，拒绝未经确认的运动，不能只凭字符串含有 109 自动放行。

数值示例（只代表编码计算，不是物理测量）：

- 6562 按新范围编码 10rad/s 为 2559，旧范围解码约 5.46849rad/s，新范围约 9.99267rad/s。
- 用旧范围编码 5N·m，若设备使用上游新范围，同一编码代表约 5.65568N·m；不是单纯显示误差。

上游还将 POS/SPD 模式的电流限幅从原始最大值 4095/65535，改为 CurMax×10 后裁剪。MIT 常用路径与两个备用模式都应覆盖。编码上界不等于额定负载、安全运行限制或工位运行增益，不能顺手放大这些限制，也不应因更新驱动而自动写电机参数。

## 3. 反馈电流与力矩必须分开

当前代码用 TauMax 解码反馈的 12 位字段，然后 backend._read_samples_impl 把 get_motor_current() 直接写进 torque_nm。采集脚本也明确输出“LRO 的该 getter 是 N·m”。

上游用独立 CurMax 解码该字段，语义变为反馈电流 A。若直接换 SDK，现有 HMI、CSV、报告、持续高力矩诊断会把安培误标为 N·m。例如 5550 的 10A 对应原始码 3185，旧解码给出 20 并标为 N·m，新解码为 10A。

必需改动：

- MotorSample、SDK getter、后端和采集脚本新增明确的 phase_current_a 或 iq_current_a；不得把它放入代表直流母线电流的 bus_current_a。
- torque_nm 只在有已核实的转矩常数、电流定义及输出轴换算关系时派生；信息不足时显示未知，不能继续用 A 代替 N·m，也不能让未知量被当作零通过依赖力矩的判定。
- 同步修改界面单位、CSV 新版本列、报告及依赖高力矩的诊断。历史 CSV 原值不可覆盖；后续分析用新版本记录换算依据和证据。

现有 motor_parameter_contract.py 已区分 kt、tor、cur，也有未确认候选范围，但 candidates-v1.json 的 applicability 为空。这说明有可复用的数据结构，不代表运行中的 SDK 已采用这些范围。不能直接拿候选 kt 作为已核实换算。

## 4. type-1 帧长度与母线数据

当前工作区已分离 type-5 查询并只让 type-1 更新遥测，但 type-1 只检查长度不少于 8；上游严格接受 8 或 12 字节。12 字节追加字段为：

- Byte8/9：大端 uint16，映射到 0～100V。
- Byte10/11：大端 uint16，映射到 -100～100A。
- 收到 8 字节帧时，母线电压/电流设为 NaN，不能保留旧 12 字节帧的数值。

本地后端已尝试调用 get_motor_dc_bus_voltage/get_motor_dc_bus_current，但原生库没有对应绑定。上游新增的是 LroMotorDriver.get_dc_bus_voltage/get_dc_bus_current，且没有暴露给 pybind 的 MotorDriver，所以只抄接收函数，界面仍可能是“—”。必须统一基类虚函数、LRO 实现、pybind 和后端 getter 名称。

合并顺序应为：先核对 ID/帧属性 → 分流 type-5 查询并按查询命令校验长度 → type-1 严格检查 8/12 → 解析观测 → 只推进一次遥测计数。不能把 8/12 长度限制放在所有查询回复之前，否则现有 20 字节版本字符串响应也会被拒绝。

温度的字节与公式没有变化：Byte6-25 是电机温度，Byte7-25 是 MOS 温度。因此本次上游差异没有给出“46℃固定偏移”或“100℃需要换公式”的证据。

0x7FF 设置帧里的使能 0x06、失能 0x07、清错 0x08、标零 0x03 命令编号也未改变；这里的命令字段与反馈 error_id 是不同字段。init_motor 仍包含真正的使能动作，HMI 应继续沿用“打开传输只读、人工使能才初始化”的分离流程，不能为了读109状态而在扫描时调用 init_motor。

## 5. 标零接口与确认状态必须适配

[f738361](https://github.com/Roboparty/roboparty_motors/commit/f738361fd9c4742301b832b33a5d780d41df6792) 把基类及各驱动 set_motor_zero 从 bool 改为 void，移除立即回读位置接近零的判断；基类注释说明新零点在电机重新上电后生效。这是上游接口意图，不是本轮现场验证。

本地 backend._zero_motor_impl 执行 bool(motor.set_motor_zero())，zeroing.confirm 看到 False 会报“未确认”。换为 void 绑定后返回 None，bool(None)=False；命令即使已发出也会被显示失败。用户可能因失败提示重复发送标零。

需要把“命令已提交、已应答、待重新上电、重新上电后核验完成”分开，并保留取消/失败证据。不能简单把 None 改成 True 后直接宣称标零成功。标零状态、网关返回和桌面提示均要对应；是否需要重新上电应按实际型号/固件核实。只迁移 LRO 协议时可通过适配层保留其他型号接口；若整体更新基类签名，DM/EVO/XYN 的 override 和测试也必须同步，避免编译或行为不兼容。

## 6. 必须保留本地已有机制

直接用上游目录覆盖 hmi/motors 会丢失这些已被 HMI 依赖的扩展：

| 本地机制 | 上游对比结果 | 合并要求 |
| --- | --- | --- |
| get_feedback_count 单调遥测计数 | 上游无该接口 | 保留；只让有效该节点遥测推进，不能退回发送/应答计数推测新鲜性 |
| request_version/version_payload 及查询关联 | 上游无该接口、接收函数忽略非 type-1 | 保留；新增详细报警查询时共用有界、可取消、按 ID/命令关联的调度，避免多个读取线程争抢回复 |
| 维护调用释放 Python GIL | 上游绑定没有本地 call_guard | 保留，避免标零/初始化等等待阻塞心跳与其他 Python 线程 |
| 原生测试传输注入与独立测试模块 | 上游没有本地扩展 | 保留，继续用无真实 socket 的协议回归 |
| SocketCAN 弱引用注册、健康标志、致命错误失效及重建 | 上游为静态 shared_ptr、没有本地健康重建逻辑 | 保留本地断开/重连契约，不能因合并协议退回旧缓存连接 |
| 缓存年龄和界面静默失效修复 | 属于 HMI 层，不在上游驱动内 | 保留；更换 SDK 不替代这些修复 |
| 可移植构建默认不使用 march=native | 上游在非交叉编译时启用 march=native | 保留本地默认，避免构建电脑指令集导致工位无法加载 |

上游 SocketCAN 增加 ROBOT_CAN_CPU（默认 CPU6）并将回调放到注册表锁外执行。这属于调度/生命周期改动，不是 109 协议必要条件。若单独借鉴，应校验可用 CPU，且保证注销/析构与在途回调同步；当前回调绑定原始 this，不能只移到锁外而忽略对象释放时序。本轮未复现实际竞态或做长时间性能测试。

## 7. 版本读取、固件维护与构建

现有 decode_version 的 0x31 字符串保留原文，但只有三段式才填 software；0x1E 则从三个字节生成三段数值版本。5.0.0.109 是四段名称，因此不能据文件名擅自把 0x1E 解释为某个确定字符串，也不能只改正则就认定两个来源相同。需要保留完整字符串与数值原始观测，用厂家证据建立明确对应和协议能力，不一致时保留 unverified。

如果同时升级电机固件，应继续使用现有固件维护及签名兼容校验。上游 OTA 请求 ID = 0x1000+2×MotorID-1、回复为请求+1；本地 motor_maintenance 已相同，不需要再改公式。上游独立 OTA 脚本增加长度/ID校验、socket 清理与错误上抛，可审阅合入；正常通信适配不要求执行 OTA，不能用该独立脚本绕过本地维护状态与固件来源校验。

完成协议适配后必须重编译匹配 Python 运行时的 motors_py 原生库，并更新 SDK 源码/库哈希。verify_native_sdk.py 明确要求 get_feedback_count、request_version、version_payload，直接拿上游二进制会不满足当前 HMI 契约。不得用只更新 Python 文件的方法交付原生协议修改；安装包按 hmi/dist 约定归档并另行核验运行实例。

## 文件级工作清单（尚未实施）

| 优先级 | 文件（相对 hmi/） | 需要改动 |
| --- | --- | --- |
| 必需 | motors/src/drivers/lro/lro_motor_driver.cpp/.hpp | 协议对应故障掩码/枚举、范围及 CurMax、8/12字节解析、母线数据、保留查询与遥测计数 |
| 必需 | motors/include/motor_driver.hpp、motors/src/pybind_module.cpp | 显式电流/母线/状态接口；统一 getter；兼容标零语义；保留 GIL 释放与现有查询 API |
| 必需 | factory_hmi/core/backend.py、factory_hmi/models.py | 区分 A 与 N·m、引入观测状态/来源、更新快照字段和标零返回处理 |
| 必需，兼容108时尤需 | factory_hmi/core/config.py、scripts/rp1_limb_common.py、create_motor_driver | 每节点协议/编码配置进入驱动构造；不只按全局库版本切换 |
| 必需 | factory_hmi/core/motor_versions.py | 四段完整固件名称与数值版本的证据对应；给兼容选择提供独立观测 |
| 必需 | factory_hmi/core/zeroing.py、desktop/app.py、相应 service/gateway 返回 | 标零待生效/待核验状态；协议对应故障名称；新电流/母线字段显示 |
| 必需 | scripts/log_motor_telemetry.py、core/report.py、core/fault_diagnosis.py | CSV/报告/诊断单位及来源一致，未知力矩不作为零或实际 N·m |
| 必需 | motors/tests、hmi/tests、tools/verify_native_sdk.py、构建清单 | 原生帧与接口契约回归，重建并记录库哈希，交付安装验证 |
| 可后续 | SocketCAN CPU配置、详细报警查询、OTA脚本 | 分别验证，不与协议核心更新混作一个未经核验的整体替换 |

## 验证覆盖和现场缺口

本轮已完成：13 个上游文本文件的 Git blob SHA 校验、与本地逐文件差异、关键历史提交审阅、故障掩码/速度/力矩/电流数值核算和 void→None→False 的接口推演。源码及结果在 hmi/dist/lro-108-109-comparison-20261030/，没有运行下载来的驱动或 OTA 脚本。

本轮没有实施上述适配，没有重编译 SDK、运行新适配回归、安装或做真实硬件验收。此前温度/快照修复的 195 项测试不是“109 已适配”的证明。

后续最低验证集：

1. 8/12字节合法 type-1、其他类型、错误长度、错误 ID，以及 enable/reserved 位不混入故障码；低三位7仍保留。
2. 四型号 MIT 单发/批发边界值，速度和反馈电流量纲；POS/SPD 电流限幅；108/新协议不同 profile 不相互污染。
3. 12→8字节切换使母线量失效，未知电流/力矩不按零处理；查询回复不推进遥测计数。
4. 标零命令已发但待核验、超时、取消及上电后核验，不因 None 报伪失败，也不将发送等同于成功。
5. 查询调度、读取异常、静默过期、心跳、退出/重连与对象生命周期回归。
6. 构建加载与来源哈希、安装版本和真实进程启动时间；最后在明确授权后做未使能实机读取及独立运动验收。

对先前温度现象的结论仍有限：上游温度公式未改，0x07 变为“其他报警”分类，单靠这份 diff 既不能认定 NTC 损坏，也不能保证迁移 109 驱动即可解决整臂温度冻结。需要对齐现场完整版本、真实编码参数及原始帧。
