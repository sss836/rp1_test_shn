# 桌面源码与本地 PLC 控制构建

本目录是本地源码和新 deb 的统一输入。`tools/build_local_deb.py` 从其所在
`hmi/` 读取源码、配置、启动器和许可文件，不读取旧安装目录的源码，不从网络拉取代码。
版本 `packaging/LOCAL_VERSION` 为 `2.0.0+ubuntu24.04`。
平台前后端、数据库及历史记录独立保留。

## PLC 能力与配置

`factory_hmi/config/plc_cabinet.yaml` 使用现场已确认的
`driver: modbus_tcp`、`access_mode: control`、`192.168.137.10:502`、Unit ID 1、RP1 V1.0。
界面提供总使能/停止、通道控制、PS1 设定值、故障复位和 AllStop。
协议身份/版本校验、控制键、心跳、事务序号确认、物理许可和原启停联锁均保留。
连接控制模式会发送控制心跳及全部输出请求关闭的安全初始化，随后等待操作员启动。
连接不是只读动作；实物负载控制需在现场确认安全状态后验收。
`access_mode: monitor` 仍受底层只读保护，禁止所有写入。
实机入口使用物理 SocketCAN；离线入口仍禁用 CAN。PLC 的 Modbus TCP 控制不依赖 CAN。

## 相对 YAML

主配置引用为 `config/plc_cabinet.yaml`，模块引用例如 `left_arm_motors.yaml`，
导入文件为 `uploaded/文件名.yaml`。网关预览、配置列表和当前生效状态显示相对引用。
内部解析保留根目录边界检查；不存在的 YAML 返回 404，越界路径被拒绝。
启动器以其文件位置确定源码根目录，以用户状态目录为工作目录，不依赖启动时所在目录。
首次启动将模板复制到 `~/.local/state/rp1-test-hmi/config/`；以后保留用户配置。
运行后修改配置应编辑此用户目录的 `plc_cabinet.yaml`，再正常重启自己的 HMI。
旧只读安装的 `rp1-test-hmi-readonly` 状态目录不被覆盖。

## 从桌面源码构建

在项目根目录运行：

```bash
python3 hmi/tools/build_local_deb.py --runtime-dir hmi/.local-runtime
```

`.local-runtime` 是已验证的 Python 3.12 离线发布运行库，不是应用源码；需事先准备，
或通过 `--runtime-dir` 指向本机已有运行库。运行库和 `dist/` 均被 Git 忽略。
输出在 `hmi/dist/`，包含 deb、SHA256SUMS、build-report.json 和包内 source-manifest.json。
固定输入时间戳由 SOURCE_DATE_EPOCH 控制；构建不启动应用或设备，无安装后自动运行脚本、
systemd 服务、免密提权规则、账号密码、私钥或现场采集 CSV。
包内包含受限 CAN 助手和需管理员认证的 polkit 动作，安装不会启动硬件。

## 启动桌面源码

在项目根目录运行 `./hmi/start-hmi.sh`；它使用本目录源码及 `.local-runtime` 离线依赖。
依赖已安装时：`python3 hmi/launch.py`。
使用离线运行库时：`python3 hmi/launch.py --runtime-dir hmi/.local-runtime`。
该命令读取实机控制配置；退出会执行原有受控停止流程。
默认网关为 `127.0.0.1:8766`，不要与旧安装同时占用同一端口。
可用 `python3 hmi/launch.py status` 查看本入口状态，用 `python3 hmi/launch.py stop` 停止本入口。

仅软件验证请运行：

```bash
python3 hmi/launch.py --runtime-dir hmi/.local-runtime --offline-check --gateway-only --duration 2 --port 18769
python3 hmi/tools/verify_local_control.py --runtime-dir hmi/.local-runtime
```

离线模式使用专用 mock 配置与状态目录，平台连接为空，不访问实物 PLC；默认验证控制模式。
支持 `--offline-access monitor` 验证只读拒绝写入。测试需要 Python 3.12。
本次同步及构建验证只使用 mock/fake transport，未启动现场控制、使能、启停或 CAN 操作。
真实负载的动作效果、急停回路和失联停止需由现场人员完成验收。

## PS1 键盘输入修复

电压、电流输入草稿在状态轮询时保留；范围或步长只有变化时才重新设置。
点击应用会在界面线程提交文本并捕获数值，再交给后台请求；编辑本身不写 PLC。
旧设定确认不能清除之后的编辑；只读、断线、运行中及命令待确认的禁用条件保留。
离线验证：`python3 hmi/tools/verify_setpoint_input.py --runtime-dir hmi/.local-runtime`。

## 物理 CAN 连接

图标名称统一为 `rp1-test-hmi`。实机入口调用 root 所有的
`/usr/lib/rp1-test-hmi/factory-hmi-can-helper`；助手以隔离 Python 模式运行，
固定读取 `/etc/rp1-test-hmi/can-policy.yaml`，拒绝用户环境替换策略文件。
策略允许 can0–can3 和原有安全速率集合，排除 lin0/lin1 和其它接口。
配置接口需正常管理员认证，不安装免密 polkit 规则或 sudoers 项。
当前左臂模板为 can0 / CAN FD，默认仲裁速率 1 Mbit/s、数据速率 5 Mbit/s。
“连接 CAN”只配置接口；扫描、使能及运动仍是独立操作。
离线回归：`python3 hmi/tools/verify_can_support.py --runtime-dir hmi/.local-runtime`。
已运行的网关需正常重启才会加载新入口；有 PLC 供电或测试时先由现场完成正常停止。

## 电机 SDK 依赖修复

本版电机 SDK 按桌面 `hmi/motors/` C++ 源码重新编译，配套
Ubuntu Noble 的 spdlog 1.12 / fmt 9 库，避免旧运行库中的 fmt 8 ABI 混用。
来源、输入源码摘要与运行库摘要记录在 `packaging/native-sdk-runtime.json`。
`tools/build_local_deb.py` 在组装包前必须通过真实 `motors_py` 加载检查；
该检查按网关导入顺序加载 NumPy、后台模块和 SDK，校验驱动 API 与实际加载的依赖，
不创建电机驱动、不打开 CAN 总线、不执行扫描、使能或运动。
离线验证：`python3 hmi/tools/verify_native_sdk.py --runtime-dir hmi/.local-runtime`。
修复 SDK 加载不等于电机硬件扫描或反馈验收完成，实物反馈需现场独立核验。

## 故障提示

全局故障同时显示实际停机异常和最高严重度诊断。共享总线的 critical 诊断
优先于单个电机的位置跟踪 warning，原反馈超时与联锁阈值保持不变。

## 跨模块标零并发

标零等阻塞式原生维护调用在 C++ 执行期间释放 Python GIL，让其它模块的控制及刷新线程继续调度。
每个模块已有的后端锁、租约及 CAN 接口独占规则保留；标零仍等待原硬件反馈，不缩短等待时间。
原生测试目标 `motors_py_testing` 复用生产绑定，创建不含任何 CAN socket 的 C++ fake motor，
验证一台 fake 标零等待 1 秒期间另一台 fake 的命令与反馈读取持续进行；测试模块不随安装包分发。
构建验证参数为 `tools/rebuild_native_sdk.py --verify-concurrency`。
