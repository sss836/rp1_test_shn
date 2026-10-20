# Ubuntu 22.04 上位机部署与验收

本次包 `2.0.12+ubuntu22.04.portable1` 从当前 `hmi/` 构建，包含大文件 NPZ 上传、
腰胯模块、样机选择及 quick2 外部供电快速验证。Ubuntu 22.04 amd64 是交付目标。
本机现有实例不会因构建而安装、关闭或升级。

## 完整运行时

`/opt/rp1-test-hmi/python3.12` 配套 `lib/python3.12` 标准库及 `lib-dynload`，
不依赖目标机安装 Python 3.12 或用户 site-packages。`runtime/` 包含 pyroute2、
PySide6、pymodbus、NumPy 以及匹配的原生 SDK，并带 libxcb-cursor 及其辅助库。
系统图形库由 DEB Depends 声明，安装时让 apt 解析；无须手工 pip 或复制到 `/opt`。
特权助手固定使用捆绑解释器 `-I`，固定导入包内代码与 runtime，读取 root 所有的
`/etc/rp1-test-hmi/can-policy.yaml`，用户 PYTHONPATH 不会替换助手或策略。

```bash
sha256sum -c SHA256SUMS
sudo apt install ./rp1-test-hmi_2.0.12+ubuntu22.04.portable1_amd64.deb
```

离线安装需提前准备 Depends 所列系统 DEB，不能把应用 runtime 完整误解为无需系统图形库。
构建与测试依赖不进入应用 runtime，文件及库版本清单随交付保存。

## CAN 与可选免密

新策略允许 can0–can5。修改过的 `/etc/rp1-test-hmi/can-policy.yaml` 是 conffile，
升级会保留本地修改；应合并新六路列表，并保留现场已批准的速率集合。
不自动覆盖现场策略。连接及恢复每一路接口时，所有允许速率统一设置发送队列 10000，
CAN FD 设置 MTU 72。安装不会改变正在运行的口。

默认需要管理员认证；要让本机处于活动会话的 sudo 组操作员免密，现场管理员可显式执行：

```bash
sudo install -m 0644 /usr/share/doc/rp1-test-hmi/examples/10-rp1-test-hmi-local-admin.pkla \
  /etc/polkit-1/localauthority/50-local.d/10-rp1-test-hmi-local-admin.pkla
```

若系统使用 JavaScript polkit 规则而不加载 pkla，则选择以下规则，不必同时安装两份：

```bash
sudo install -m 0644 /usr/share/doc/rp1-test-hmi/examples/49-rp1-test-hmi-local-admin.rules \
  /etc/polkit-1/rules.d/49-rp1-test-hmi-local-admin.rules
```

这两份示例仅允许专用 CAN action；远程、非活动用户和普通账号不获此授权。
撤销时删除所选规则即可，原 CAN allowlist 和安全速率校验始终保留。

## 窗口与有效配置

桌面图标直接运行 `rp1-test-hmi`，无需另外维护本地包装。窗口首次创建后请求抬窗，
再次点击图标会向当前实例发送抬窗请求。最小化窗口恢复，已移走的显示器上的窗口移回主屏。
窗口恢复仍受桌面环境的焦点策略约束，HDMI 多屏需现场验证。

工位配置默认位于 `~/.local/state/rp1-test-hmi/config/stations/`，
`--state-dir` 或 `XDG_STATE_HOME` 会改变该位置；模块参数窗口显示当前目录。
`hmi/scripts/config/` 和 `/opt/.../hmi/scripts/config/` 是首次初始化模板，
重装不会覆盖用户工位配置。导入 YAML 会生成 stations/uploaded 下的新文件，
应以界面当前配置引用为准，修改后重新应用对应模块。

上身模板维持原默认姿态与安全阈值：速度 8 rad/s，首帧差 0.75 rad。
未取得并核对现场 `run_upper_body_loop.npz` 前，不统一写成 12 rad/s 或猜测首帧。
如现场已验证某工位的轨迹需要 12 rad/s，可在该工位有效 YAML 设置：

```yaml
factory_hmi:
  safety:
    max_velocity_rad_s: 12.0
```

默认姿态 `joint_default_angle` 必须按该 YAML 的关节顺序和 motor_sign/zero_offset
定义与实际轨迹对应。导入阶段检查文件内速度、帧差等；进入 Armed 时再次按实时电机反馈
检查首帧差，不能只把默认姿态改成首帧就假定机器人已到该姿态。
保留受控回默认位及预检，长时运行使用无缝循环轨迹配合测试时长。

## 腰部与温度

没有修改 playback 完成或失能语义来实现腰部保位；model 3 的 MIT 停后行为需按固件实测。
手臂 LRO 温度继续按原始温度字节减 25 解码，不更改采集事实。
新鲜反馈同时出现 error_id=7 时，界面明确提示 DRV_ERROR 以及温度可能冻结，
显示值仍是设备上报值；过期反馈继续显示为空。此提示不等于确认某固件一定冻结温度。

## 升级与现场验收

先停止测试、支撑负载、完成电机失能和 PLC 正常总停止，确认实际反馈后正常关闭旧实例。
用同一状态目录查询 `rp1-test-hmi status`，确认 `running=false` 后安装。
安装后核对版本、进程启动时间、源码清单和网关健康，再执行现场验收。
完整步骤见 [本地控制构建说明](LOCAL_CONTROL_BUILD.md)。

验收包含：HDMI 可见且重复点击能恢复窗口；can0–can5 的应用/清除；所选免密规则生效；
队列实际值及 CAN 错误/丢包计数；上身与腿部并行运行的反馈年龄与控制延迟；
目标轨迹预检；腰部固件停后行为。六路 USB2 与小队列只是已有现场记录，
不能凭 480 Mbps 或队列加大认定卡顿根因已完全排除，仍需实际负载和至少 5 小时稳定性验收。

## 可复现构建

在隔离 Ubuntu 22.04 中，以只读 `/src` 挂载 `hmi/`、空 `/out` 接收输出，运行
`tools/build_jammy_runtime.sh`。它编译当前电机源码，固定主要 Python 库版本，
生成完整标准库、cursor 库和 SDK 来源清单，并拒绝高于 glibc 2.35 的运行库。

```bash
python3 hmi/tools/build_local_deb.py \
  --runtime-dir /构建输出/runtime --python-binary /构建输出/python3.12 \
  --python-stdlib /构建输出/lib/python3.12 --depends-profile jammy \
  --native-provenance /构建输出/native-sdk-runtime.json \
  --version 2.0.12+ubuntu22.04.portable1 \
  --output-dir hmi/dist/本次批次
```

交付包含 DEB、SHA256SUMS、构建报告、源码与运行时清单、离线回归及干净系统启动记录。
软件 mock 验证不代替硬件联调；构建报告中安装与现场验收状态单独记录。
