# RP1 TEST HMI（PLC）

Ubuntu 24.04 amd64 上位机源码。新机安装、平台部署和账号接入请从 [根目录 README](../README.md) 开始。

- 当前平台模式：`reliability_v1`；网关 `127.0.0.1:8766`。
- 在线/运行监控：授权账号登录后每 10 秒上报，原始采集 CSV 留在本地，支持并行测试和退出账号。
- 仅本地测试可不选择上传上下文；在线监控不要求旧平台 machine token。
- 新平台的测试数据审批流程尚未实施，旧“提交审批”按钮不等于审批链路已完成。
- 配置 `/etc/rp1-test-hmi`，记录与 CSV `/var/lib/rp1-test-hmi`。
- 默认 mock PLC，真实 PLC/CAN 控制须完成现场配置与硬件验收。

[监控与升级](docs/PLATFORM-PRESENCE.md) · [PLC 实现](docs/PLC_CABINET_IMPLEMENTATION.md) · [PLC 映射](docs/PLC_INTERFACE_MAPPING.md) · [Modbus 协议](docs/HOST_MODBUS_TCP_PROTOCOL_V1.0.md)

源码构建：在仓库根执行 `sudo bash scripts/install-build-deps-ubuntu24.sh`，再执行 `./scripts/build-hmi.sh`。
