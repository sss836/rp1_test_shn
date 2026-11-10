# RP1 PLC 上位机 v3.0.27

本版提供 Ubuntu 22.04 和 24.04 的 amd64 安装包。默认平台地址为 `https://10.43.17.214:8443`。升级保留工位已有的平台地址、证书及本地记录。

- 工厂快速验证正常结束时补完当前一圈再收尾。手臂默认按经现场确认的下垂姿态回位；其他部位按已核实的工位参数回位。到位并确认反馈稳定后失能。未核实停车参数的工位继续保持位置。
- 每次快速验证独立记录测试项目编号、完整的本次测试原始采样 CSV、圈数、有效时长、结束原因、故障和关键指标；收尾证据独立保存。平台归档仍走核实、提交和审批。
- 工厂快速验证和普通轨迹测试的文件峰值速度只供参考。实际电机反馈超速保护、位置和加速度限制、首帧接近和收尾限速继续生效。

实现与软件验证边界见 [工厂快速验证与新开窗口](../hmi/docs/QUICK_VERIFICATION.md)。

## 安装包与源码

[GitHub Release](https://github.com/sss836/rp1_test_shn/releases/tag/v3.0.27) 包含对应系统的 DEB、`SHA256SUMS` 及构建报告。源码由 `v3.0.27` 标签固定。Ubuntu 22.04 包内置便携 Python 3.12；Ubuntu 24.04 包使用系统 Python 3.12。

## 升级

先确认测试已结束、电机已失能、PLC 已反馈停止，再正常关闭旧上位机。在原工位状态目录下运行 `rp1-test-hmi status`，确认 `running=false`，然后按系统选择以下一组命令。

Ubuntu 22.04 amd64：

```bash
base=https://github.com/sss836/rp1_test_shn/releases/download/v3.0.27
deb=rp1-test-hmi_3.0.27+ubuntu22.04_amd64.deb
curl -fL --retry 3 -o "$deb" "$base/$deb" &&
curl -fL --retry 3 -o SHA256SUMS "$base/SHA256SUMS" &&
awk -v deb="$deb" '$2 == deb' SHA256SUMS | sha256sum -c - &&
sudo apt install "./$deb"
```

Ubuntu 24.04 amd64：

```bash
base=https://github.com/sss836/rp1_test_shn/releases/download/v3.0.27
deb=rp1-test-hmi_3.0.27+ubuntu24.04_amd64.deb
curl -fL --retry 3 -o "$deb" "$base/$deb" &&
curl -fL --retry 3 -o SHA256SUMS "$base/SHA256SUMS" &&
awk -v deb="$deb" '$2 == deb' SHA256SUMS | sha256sum -c - &&
sudo apt install "./$deb"
```

安装后用 `dpkg-query -W rp1-test-hmi` 检查版本，从桌面入口启动，再用同一状态目录运行 `rp1-test-hmi status` 核对新进程与网关状态。模拟验证不代替真实部位的停放姿态、撤力和 PLC/CAN 验收。
