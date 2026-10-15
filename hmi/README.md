# RP1 TEST HMI（PLC）

Ubuntu 24.04 amd64 上位机源码。新机安装、平台部署和账号接入请从 [根目录 README](../README.md) 开始。

- 当前平台模式：`reliability_v1`；网关 `127.0.0.1:8766`。
- 在线/运行监控：授权账号登录后每 10 秒上报，原始采集 CSV 保留本地副本，支持并行测试和退出账号。
- 启动必须选择正式用例或“其他”；“其他”必须填写测试名称、实际对象和工位，可以先归档后归类。在线监控不要求旧平台 machine token。
- 源码已实现核实、专用 CSV 归档、审批及驳回重提；完整链路需配套平台迁移、归档 Worker 与新版 HMI 部署。详见 [中央归档与曲线分析](../docs/CSV-ARCHIVE-IMPLEMENTATION.md)。
- 配置 `/etc/rp1-test-hmi`，记录与 CSV `/var/lib/rp1-test-hmi`。
- 默认 mock PLC，真实 PLC/CAN 控制须完成现场配置与硬件验收。

[监控与升级](docs/PLATFORM-PRESENCE.md) · [PLC 实现](docs/PLC_CABINET_IMPLEMENTATION.md) · [PLC 映射](docs/PLC_INTERFACE_MAPPING.md) · [Modbus 协议](docs/HOST_MODBUS_TCP_PROTOCOL_V1.0.md)

源码构建：在仓库根执行 `sudo bash scripts/install-build-deps-ubuntu24.sh`，再执行 `./scripts/build-hmi.sh`。
# 本机最新源码与 PLC 实机控制

当前本地控制版本、相对 YAML、源码启动和从本目录生成 deb 的入口见
[LOCAL_CONTROL_BUILD.md](docs/LOCAL_CONTROL_BUILD.md)。新打包命令为
`python3 tools/build_local_deb.py --runtime-dir .local-runtime`；不会启动设备。

多窗口 PLC 共享控制已加入请求版本校验、幂等回执、冲突拒绝和全部停止优先，两个窗口均可控制。详见 [多窗口控制规则](docs/LOCAL_CONTROL_BUILD.md#两个窗口共同控制-plc)。
