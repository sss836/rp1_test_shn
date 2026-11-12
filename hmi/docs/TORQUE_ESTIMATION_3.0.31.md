# v3.0.31：109 估算力矩

## 问题与数据语义

local16 的 109 分支已解析 Iq，但将 torque_nm 设为 NaN。因此电机监控、老化峰值和 CSV 没有力矩。现场旧 CSV 已确认有 Iq；本次不会回写旧 CSV 或伪造历史力矩。

109 估算力矩 = 当前电机 Iq(A) × 当前电机读回的 Kt(N·m/A)。四款型号 LRO_5550、LRO_6562、LRO_8462、LRO_10062 均走同一公式，系数按配置型号、CAN 接口和 ID 绑定到各自实际响应。不使用产线模板的目标 Kt 替代实际值，不额外乘减速比；该值是依赖驱动参数和电流反馈的估算，不是外部力矩传感器测量。108 沿用原有反馈/换算逻辑。

## 文件与调用链

- `motors/src/drivers/lro/lro_motor_driver.cpp::request_version`：在现有只读查询白名单增加 0x16。仍发 E0 16，不发送参数写入、使能或运动命令；接收按同一接口实例、CAN ID、查询码及 500 ms 窗口关联，参数响应不增加运动反馈计数。
- `core/motor_versions.py::MotorVersions._run`：现有每连接一个后台线程，在版本识别为 109 后串行读取 Kt。顺序与版本请求共用，避免竞争 pending 查询。每轮约 60 秒刷新，单次最多 500 ms，停机取消等待；不会在 UI 或 200 Hz 控制循环中等待参数。108 不新增此查询。
- `core/torque_estimation.py`：按用户产线脚本的大端无符号 /100 编码解析 Kt；验证正值、目标身份和 180 秒有效期。每次重连重新读取。读取失败、未知型号或过期时显示缺失及原因，原始 Iq 继续提供。
- `core/backend.py`：消费缓存系数，计算每个反馈样本。固定 108/109 控制协议表、命令编码和使能逻辑保持不变。更换配置、固件或参数维护重连时不复用旧连接系数。
- `models.py`：兼容内存中的 torque_nm，同时附带 torque_is_estimated、torque_kt_nm_a、torque_source 和 estimated_torque_nm。
- `desktop/app.py`：109 数值与峰值显示“估算”；监控表提供实测 Kt，保留 Iq。Kt 缺失明确显示原因。108 普通数值显示不变。
- `core/playback.py`：计算估算力矩绝对峰值/均值，保存估算标记、有效/缺失力矩样本数及 Iq 峰值。
- `scripts/log_motor_telemetry.py`：CSV 分别保存原始 iq_current_a、实际 torque_kt_nm_a、estimated_torque_nm 和 torque_is_estimated。109 不向原有 torque_nm 列填入估算值；108 继续保存原列。高频记录使用后台缓存快照，不查询 CAN。
- `core/telemetry_hub.py`、`gateway/local_curves.py`、`frontend/src/local-preview/model.ts`：实时和历史曲线使用明确的估算力矩通道，曲线标签含“估算力矩”；Iq、Kt 独立通道及单位。缓存仍限制 20 Hz、60 秒、1200 帧和 4 MB。
- `core/streaming_metrics.py`、`core/report.py`：历史摘要优先使用原力矩，缺失时使用已保存的估算列；JSON 与报告明确标识。未保存 Kt 的旧数据不补算。派生窗口保留原字段名称。

## 边界

控制范围仍使用现有 108/109 固定表；本次只补齐力矩观测，不改电机参数、轨迹或保护阈值。既有持续高负载诊断获得估算值时会标明估算来源。系数变动按收到新响应后的样本生效，每行 CSV 保存当时系数，不追溯重算原始行。

平台登录和在线监控独立；本地换算、曲线和记录无需登录。平台软件更新入口只发布安装包；本轮不自动上传现场测试记录，也不改变 reliability_v1 归档/审批要求。

最终测试、安装、GitHub 和平台发布结果见同批构建报告。本轮软件测试使用模拟设备，不能替代真实四型号动作和力矩标定验收。
