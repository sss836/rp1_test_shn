# RP1 PLC 上位机 v3.0.26

本版提供 Ubuntu 22.04 和 Ubuntu 24.04 的 amd64 安装包。默认平台地址为 `https://10.43.17.214:8443`。已有工位显式配置的平台地址和证书由原有升级机制保留。

- 每个窗口在独立 Qt 进程中运行，各工位分别运行电机控制、CAN、租约和 CSV 记录。正常关闭一个工位时，另一工位可继续运行。
- 两个工位共用一个 PLC 柜进程。柜进程统一处理供电指令和回执。用于 PLC 供电的快速验证会在工位内检查对应通道的实际供电许可。
- 界面读取最新快照，报告在 CSV 收尾后由独立进程生成。详细边界和隔离验证见 [双工位改造实现与验证](../hmi/docs/HMI-DUAL-STATION-IMPLEMENTATION-20261110.md)。

## 安装包与源码

从 [v3.0.26 Release](https://github.com/sss836/rp1_test_shn/releases/tag/v3.0.26) 下载对应系统的 DEB 和 `SHA256SUMS`。标签 `v3.0.26` 固定发布源码。Ubuntu 22.04 包包含便携 Python 3.12 和标准库，Ubuntu 24.04 包使用系统 Python 3.12。源码构建见 [HMI 构建说明](../hmi/README.md) 和 [Ubuntu 22.04 部署说明](../hmi/docs/UBUNTU22_DEPLOYMENT.md)。

## 升级

在原工位状态目录下，确认测试停止、电机已失能以及 PLC 实际停止反馈，再正常关闭旧上位机。运行 `rp1-test-hmi status`，确认输出中 `running` 为 `false` 后执行对应系统的命令。

Ubuntu 22.04 amd64

```bash
base=https://github.com/sss836/rp1_test_shn/releases/download/v3.0.26
deb=rp1-test-hmi_3.0.26+ubuntu22.04_amd64.deb
curl -fL --retry 3 -o "$deb" "$base/$deb" &&
curl -fL --retry 3 -o SHA256SUMS "$base/SHA256SUMS" &&
sha256sum --ignore-missing -c SHA256SUMS &&
sudo apt install "./$deb"
```

Ubuntu 24.04 amd64

```bash
base=https://github.com/sss836/rp1_test_shn/releases/download/v3.0.26
deb=rp1-test-hmi_3.0.26+ubuntu24.04_amd64.deb
curl -fL --retry 3 -o "$deb" "$base/$deb" &&
curl -fL --retry 3 -o SHA256SUMS "$base/SHA256SUMS" &&
sha256sum --ignore-missing -c SHA256SUMS &&
sudo apt install "./$deb"
```

安装后运行 `dpkg-query -W rp1-test-hmi` 确认版本。从桌面入口启动上位机，使用同一状态目录运行 `rp1-test-hmi status` 核对新进程，并确认界面能接收网关状态。模拟验收覆盖软件隔离，现场仍需复核真实设备的双工位运行与停机反馈。
