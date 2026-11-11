# CAN 连接后提示工位此前请求未确认

## 已确认的证据

现场截图的弹窗为 HTTP 503，详细信息是“工位仍在处理此前请求，结果未确认”。同一截图底部显示 can0 为 error-active，电机未发现、未使能。用户反馈执行接口 down、设 bitrate=1000000、up 和 candump 后恢复。没有取得恢复前的实际 netlink 详情和出错工位日志，不能将 bus-off、CAN/FD 模式或波特率不匹配写成现场已证实根因。

error-active 是 CAN 控制器状态名称，本身不表示 CAN 已故障。candump 读取总线流量，不负责修改接口参数。down/configure/up 会重新初始化接口，但只写 bitrate 并不证明其 CAN-FD 标志被关闭。

## 代码层含义

v3.0.27 的 gateway/station_process.py::_Channel.call 在请求超时后保留 pending。下一次调用先尝试收取之前的回复；之前的回复仍不可读时抛出截图中的错误。gateway/app.py 将 StationUnavailable 映射为503，界面为此显示“连接 CAN 失败”。

工位控制通道预算为120秒，只读通道1秒，租约通道0.3秒。CAN提权helper另有15秒预算。上述通道都会经过 _Channel，弹窗文本不携带channel/previous-operation，因此只凭文字不能辨认哪条操作拖延。连接请求在设备动作完成后还读取快照和租约，回执链超时也可能使成功的设备操作在界面显示为失败。

隔离Socketpair复现：无worker回复时，第一次只读snapshot请求显示“工位进程请求超时，结果未确认”，第二次显示“工位仍在处理此前请求，结果未确认”。复现没有连接CAN或PLC，证明错误由进程回执状态触发，不证明现场硬件故障原因。

## 现场避免与取证

1. 核对“模块参数”的CAN/FD模式、bitrate和dbitrate与恢复后实际接口一致。不要只比较1Mbps，也不要只凭手动命令将模式改成经典CAN。
2. 同一个接口由一个配置入口管理，避免上位机与外部启动脚本或调试工具交错执行down/up。candump仅监视，不要求关闭它才能测试。
3. 有“此前请求未确认”时先观察实际状态，避免反复点击连接。需要重新配置接口时，先停止测试并确认电机失能，再通过正常断开/关闭流程处理。
4. 复发时先保存恢复前后的 `ip -details -statistics link show can0`、安装版本、内核CAN/USB事件与工位日志。重点核对bus-off计数、tx/rx错误、模式和实际时序。

## 软件改进建议（本次尚未实施）

连接操作回执与随后状态刷新分别报告；只读刷新慢时提示状态待刷新，避免把它直接呈现成动作失败。为pending记录通道、方法、请求编号、开始时间和耗时，以便区分等待授权、netlink、锁与回执。只读状态可使用带新鲜度的缓存，控制动作结果未知时继续保留互斥和确认流程，不直接清空pending重发设备动作。CAN初始化校验模式和时序，匹配且健康时避免重复重置。

## 与旧版的代码差异和逻辑修正范围

双工位改造提交 `c9a1b5a` 新增了 station_process.py，在v3.0.26正式发布中启用。旧的同进程版本由gateway直接调用FactoryService，没有此文件的IPC pending状态和“工位仍在处理此前请求”错误。旧版仍可能发生接口故障、HTTP超时或界面等待，不能从旧版没有这句提示推断设备层从未延迟。

新版把工位放入不同进程，控制和只读请求已分通道，但worker内部的状态采集与CAN连接仍共享FactoryService._lock。can_connect持有该锁等待helper/netlink，collect_display_snapshot和display_snapshot也会取它。连接可能合法耗时超过1秒，期间只读RPC先超时并保留pending；连接完成后，gateway mutate还会读取display_snapshot，之前未完成的只读回复能再次触发503。这条路径把“显示状态未刷新”与“设备动作失败”混在同一HTTP返回中。

隔离实验进一步用实际FactoryService的锁和_Channel模拟上述竞争：持有初始化锁时，display_snapshot第一次超时、第二次出现截图文字；释放初始化锁后状态读取恢复且pending清除。没有启动真实PLC/CAN。实验复现了代码路径，现场最初触发的具体阻塞仍须用操作日志判定。

应从以下调用边界修正，而非仅放大总超时：

| 代码位置 | 修正逻辑 |
| --- | --- |
| core/service.py::can_connect | 小锁只冻结配置、revision和连接状态；使用独立生命周期/设备操作互斥执行helper与netlink；操作完成后再短锁发布结果，旧配置epoch不接受晚结果 |
| gateway/app.py::mutate | 成功动作按独立回执返回，不强制等待新的显示快照；显示缓存注明采集时间和过期，状态缺失不改写已确认的动作结果 |
| gateway/station_process.py::_Channel | 请求带唯一编号，独立接收与匹配晚回执；只读超时与动作结果未知分别表达，控制结果未知时查询原回执，禁止自动重发 |
| desktop/app.py::_connect_can | 一次点击对应一个操作编号；处理中保持单在途并查询进度，明确显示已连接、等待确认、真实失败三种状态 |
| core/can_manager.py | 校验实际UP状态、CAN/FD模式及时序；健康且完全匹配时复用，需重配时遵循工位生命周期，不因状态刷新慢反复down/up |

验收需注入慢初始化、慢状态、迟到/分片回复、真实helper失败和双工位并发，分别验证命令只执行一次、回执不串号、状态慢不误报连接失败、CAN0慢不挡CAN1及心跳。以上逻辑修正尚未实施。

参考：[Linux SocketCAN](https://docs.kernel.org/networking/can.html)、[candump手册](https://manpages.debian.org/bookworm/can-utils/candump.1.en.html)。
