# 已核实参数与正常受控收尾（源码实施，未现场生效）

2026-10-29 修订：按老板选用此前 MuJoCo 轨迹的要求，2.0.19 新增独立的 `mujoco_hanging` 收尾方式，支持左右 5/7 自由度手臂。普通和工厂测试在开始前明确选择并确认模型姿态适用及撤力安全后，可补完末轮、接续内置下垂轨迹、等待实际位置与速度稳定后软件失能；不要求给旧工位 YAML 填入伪造的 verified 参数。既有参数收尾及未选用时的保持逻辑保留，详见 [MUJOCO_COMPLETION.md](MUJOCO_COMPLETION.md)。模型来源、软件到位判据和现场确认分别记录，尚未执行真机回位验收。

2026-10-28 修订：按老板“原本预检正常，马上修复、安装后正常使用”的最新要求，缺少自动收尾参数不再阻止原有轨迹预检和测试。原有实时首帧、限位、速度及故障保护保留；未配置自动收尾时，测试结束后持续位置保持，不自动回位或失能。工厂模式同样保留本窗口 CAN 占用和保护监控。显式 verified 配置如果校验失败仍阻止启动，不静默降级。下文“缺参禁止所有 arm/start”属于修订前记录，以本段及 [NORMAL_USE_PREFLIGHT_FIX.md](NORMAL_USE_PREFLIGHT_FIX.md) 为准。

## 当前边界

源码新增了共用正常收尾状态机。产品的机械/线缆限位、停放姿态（含 elbow 自然下垂）、速度/加速度、容差、稳定窗口和超时仍未获得核实资料。因此所有未提供完整核实配置的部位都拒绝 arm/start；没有把旧全零默认位当成安全停放位。2026-10-28按用户明确指定，已将左右5DOF工厂资源原样替换为 `left_short_arm_joint_inspect.npz` / `right_short_arm_joint_inspect.npz`，manifest升级v2；未生成新的角度数据、未改自定义轨迹。

`hmi/config/examples/motion_completion.unverified.yaml` 只含空值和 false，不能用于启用设备。测试中的 `SYNTHETIC-NOT-PRODUCT-1` 是隔离假设备参数，禁止复制为产品参数。

## 文件与调用链

- `core/motion_completion.py`：`MotionParameters.load` 检查核实标记、证据引用、部位、可安全撤力确认及配置哈希；全部关键参数必须显式提供有限值。`check_trajectory` 检查映射后的关节/电机限位、首帧实际误差、离散速度/加速度、人工结束的制动余量及循环首尾连续性。`motion_readiness` 返回准入原因和有效配置哈希。
- `core/playback.py`：`PlaybackController.finish(reason)` 只设置线程安全事件。正常到时标记 `finishing_cycle`，不再开新周期；整个当前周期播放完后受控制动、慢回停放位、验证实际反馈持续稳定，再调用 `disable_motors`。单次/循环次数结束使用相同收尾。人工结束先按各轴核实加速度减速，不补余下周期，结果 `stopped`，不自动当成通过。
- `core/controller.py` / `core/service.py`：新增 `finish`，原 `stop` 保持保护中断用途。成功收尾已完成失能，不再启动旧的完成后位置保持。普通测试保留连接。
- `gateway/quick_run.py`：启动前先查参数 readiness，缺参时在供电或 CAN 连接前拒绝。移除旧“全零默认位 3 秒”的自动动作；首帧实际误差不满足时 arm 拒绝。`end` 请求正常收尾并立即返回，watcher 持续监控收尾期间供电/租约；只有 `completed` 且 `completion.safe_disabled=true` 才清理并释放本窗口 CAN。`abort` 用于未确认启动、失能等保护取消，立即走保护清理；不改变共用 PLC 供电和其他窗口 CAN。
- `core/motion_tools.py`：完全离线读取配置与 NPZ；提供哈希、映射审计和显式生成新的 5DOF J5 派生轨迹，不启动设备。

## 状态、计时与异常

`playback.completion` 包含 `phase/active/safe_disabled/parameter_revision/return_seconds/settling_seconds/alarm/evidence_path`。阶段可为 `running`、`finishing_cycle`、`decelerating`、`returning`、`settling`、`needs_attention`、`safe_disabled`、`interrupted`、`fault`。

到时后的 NPZ 补尾计入 `progress.active_seconds/completed_cycles` 并继续写测试 CSV。人工减速在进入回位前结束测试采样。自动周期尾部的减速、回位、稳定检查不计入测试 CSV 或测试统计；回位计时独立。记录器的数据源在测试边界冻结，已经取得的一条在途记录也只含边界前样本。`ended_at` 为测试采样结束时间。

回位与稳定依据实际电机位置误差、实际速度、反馈新鲜度、错误码及连续稳定窗口，且每轴反馈时间戳必须实际推进覆盖该窗口，不能用一个重复缓存样本累计稳定时间，也不能仅凭目标命令判断到位。目标健康但超时未到位时保持并报警；超时后不自动转成成功，保留 CAN 占用，等待人工保护处理。超时会写单独的 `.needs_attention.json`；终态再写执行唯一的本地完成证据。软件失能、急停链路、租约、通信/电机故障可中断上述阶段，不等待回位。现有后端控制心跳监督仍独立工作。

本地证据在服务数据目录 `motion-completion/<uuid>.json`，不加入测试 CSV、封存测试包或自动上传；不修改 reliability_v1 核实、归档和审批。直接使用核心类时可显式提供 `completion_output`；默认核心证据目录为 `~/.local/state/rp1-test-hmi/motion-completion/`。写入失败会反映在 completion.alarm。

暂停仅适用于测试运行/补尾阶段，保持当前命令并冻结 active_seconds；进入减速/回位后拒绝暂停，独立软件失能仍可抢先执行。

## 参数核实及哈希

每部位由有依据的机械/设备资料和现场确认填写模板；特别要核实自然下垂与安全撤力，不能据旧 `joint_default_angle: 0` 推导。配置哈希覆盖去掉 motion_completion 本身后的完整配置、有效电机条目（包括 GUI 生效的 sign/zero_offset/CAN 等覆盖）及控制频率。变更映射、零偏、接线或配置后必须重新核实；不能只沿用旧 verified=true。

网关实际快照 `station.motion_readiness.configuration_sha256` 是生效配置的摘要。离线核对可执行：

```bash
PYTHONPATH=hmi python3 -m factory_hmi.core.motion_tools hash \
  --limb right_short_arm --config /path/to/verified-station.yaml --interfaces can1
```

离线工具的哈希仅在配置、频率、CAN 绑定和 GUI 覆盖与运行时一致时可直接使用；有 GUI 电机覆盖时以实际快照核对。参数中的速度/加速度及到位误差、速度容差采用电机空间每轴值，关节位置上下限与停放位采用关节空间弧度。离散 NPZ 导数检查不等于真实硬件动力学验证。

## J5 核对与生成

先前 v1 资源审计保留在历史交付目录 LEFT/RIGHT-5DOF-AUDIT.json。当前 v2 两条资源来自用户明确选择的 `user-supplied/motion/arm/{left_short_arm_joint_inspect.npz,right_short_arm_joint_inspect.npz}`，原样复制，来源SHA256和被替代SHA256写入manifest。当前资源校验见 `outputs/animal-office/hmi-v1-completion/motion/RESOURCE-CHECKS.json`。两文件均633帧/50Hz/12.64秒，首尾命令零不等于自然下垂或可安全撤力证明。左右 sign、零偏与实际反馈仍须按现场配置核实。

核实配置齐全后，显式提供目标范围生成新文件，例如（MIN/MAX 必须来自核实资料）：

```bash
PYTHONPATH=hmi python3 -m factory_hmi.core.motion_tools extend-j5 \
  --limb left_short_arm --config /path/to/verified-station.yaml --interfaces can0 \
  --input /path/to/source.npz --output /path/to/new-version.npz \
  --minimum-j5-rad MIN --maximum-j5-rad MAX
```

生成工具保留原始帧，并在末尾追加仅改变 J5 的平滑双向段，最后回到追加前的 J5 值。其余关节保持原范围；只支持左右 5DOF，不处理 7DOF。源文件、既有输出和来源证据均不可覆盖；旁置 `.provenance.json` 保留源/新哈希、参数版本及 `physical_effect_verified=false`。不自动更新工厂 manifest，不自动替换自定义轨迹。若源轨迹或追加段不满足核实限位/速度/加速度，生成失败。

## 已做与未做的验证

`hmi/tests/test_motion_completion.py` 使用显式 synthetic 核实配置和 FakeMotorBackend，未创建 PLC 服务或真实 CAN 后端。覆盖参数门禁、补尾计数和记录边界、人工结束、暂停、回位未到位保持、故障抢先、循环、J5 新文件生成与工厂所有权。

未核实产品参数、未验证真实 J5 活动、未安装或核对当前运行实例、未启动真实 PLC/CAN、未执行长时稳定性试验。源码自检不等于现场验收；后续由老板判断交付是否合格，并另行授权安装和真实设备验证。


2026-10-28指定资源回归：`test_selected_factory_trajectories.py`直接读取左右所选真实NPZ，使用显式synthetic限位和非零停放位、FakeMotorBackend及虚拟单调时钟，按未改动的1倍原时间轴核对补完12.64秒末周期/第632帧、慢回、实际误差/速度及反馈时间戳稳定后失能；并验证未到位保持报警、电机故障优先。共用运动回归和指定资源测试合计23项通过；这不是实际12.64秒设备运行或真实防摔验收。
