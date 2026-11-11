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

参考：[Linux SocketCAN](https://docs.kernel.org/networking/can.html)、[candump手册](https://manpages.debian.org/bookworm/can-utils/candump.1.en.html)。
