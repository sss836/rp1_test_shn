# RP1 Test SHN — Reliability Platform 与 RP1 TEST HMI（PLC）

当前工程版本 **2.0.0**，目标系统 **Ubuntu 24.04 LTS amd64**。包含当前可靠性平台的 React 前端、FastAPI 后端、PostgreSQL 数据库、MTBF Worker，以及 PLC/CAN 上位机完整源码和构建工具。仓库采用 `reliability_v1` 协议；不是旧版 dashboard 平台。

**已实现：**账号与权限、可靠性数据查询/计算、上位机登录在线监控、并行测试状态、运行时长/循环数、PLC 通信状态、断线与过期状态提示。

**尚未实现：测试数据提交→后台审核→批准后发布正式统计。** 账号权限申请审批与测试数据审批不同。旧 HMI 的“提交审批”文案和既有 ingestion 适配器不能视为新平台已具备数据审批；不要使用该旧提交链路作为正式审批交付。当前发布用于已验证功能的工程部署，不宣称完整商业验收完成。监控仅传有限结构化状态，原始采集 CSV 保留在工位本地，不上传 GitHub，也不经监控接口上传。

## 1. 目录与部署拓扑

| 目录 | 内容 |
|---|---|
| `frontend/` | React/Vite：测试总控、时长台账、MTBF、样品中心、工位监控、账户管理 |
| `backend/` | FastAPI `/api/v1`、Cookie/CSRF 认证、权限与 MTBF Worker |
| `database/` | PostgreSQL 16、Alembic 迁移、静态测试目录与规则 |
| `hmi/` | PySide6 桌面、本机网关、PLC Modbus TCP、SocketCAN、报告与本地记录 |
| `scripts/` | 新机配置、启动、验收、HMI 构建、备份与恢复 |
| `docs/` | [监控实现](docs/HMI-PRESENCE.md)、[运维](docs/OPERATIONS.md)、[验收边界](docs/ACCEPTANCE.md) |

浏览器/HMI → HTTPS 8443 → Nginx → API → PostgreSQL；独立 Worker 处理 MTBF 重算。HMI 桌面通过本机 `127.0.0.1:8766` 连接网关，网关连接 PLC/CAN。数据库不公开主机端口。本版本不依赖 InfluxDB 或 NAS 服务。

`database/imports/test_cases_*.csv` 是迁移需要的静态测试项目定义，不是采集原始 CSV；初始化含测试目录，不含旧电脑真实执行历史。不会自动载入 showcase 示例测试记录。

## 2. 全新电脑准备

平台建议至少 4 核 CPU、8 GB 内存、100 GB 可用 SSD；同时运行 HMI 建议 Ubuntu Desktop 与 16 GB 内存。真实工位还需匹配的 CAN 适配器、PLC 网口及已验收设备。ARM、Ubuntu 22.04、Windows 不属于此安装包的验收范围。

```bash
sudo apt update
sudo apt install -y git ca-certificates openssl python3 curl
git clone https://github.com/sss836/rp1_test_shn.git
cd rp1_test_shn
git checkout v2.0.0
sudo bash scripts/install-docker-ubuntu24.sh
sudo usermod -aG docker "$USER"
```

注销并重新登录，然后返回仓库，确认 `docker version` 与 `docker compose version` 正常。Docker 组仅授予部署管理员。生产工位固定验收版本，不自动跟随 main。

## 3. 配置并启动平台

以下两种配置**二选一**：

```bash
# 同机，仅本机访问
python3 scripts/configure.py

# 或：供局域网使用，把示例地址换成平台服务器实际固定 IP / DNS
python3 scripts/configure.py --hostname 192.168.10.20 --bind 0.0.0.0 --port 8443
```

配置脚本生成随机数据库/管理员凭据、`.env`、本地 CA 和 HTTPS 证书，拒绝覆盖已有配置。**不要复制原电脑 `.env`、数据库或私钥作为新站点默认配置。** `deployment/secrets/` 与 `.env` 均不进入 Git。

在浏览器和 HMI 电脑上信任站点 CA；分机时仅安全传递 `root-ca.crt`，不要复制 CA 私钥：

```bash
sudo cp deployment/secrets/root-ca.crt /usr/local/share/ca-certificates/rp1-site.crt
sudo update-ca-certificates
./scripts/up.sh
```

浏览器如使用独立证书库，还需在其证书颁发机构设置中导入同一 `root-ca.crt`。使用配置时的 IP/DNS 访问，不能靠关闭证书校验解决名称不匹配。

启动会构建镜像、创建独立 PostgreSQL 卷、执行迁移、启动 API/Worker/Web，随后运行只读检查。首次构建需要 Docker Hub、PyPI、npm 网络。打开 `https://localhost:8443`，或配置的局域网地址。

```bash
# 仅在管理员自己的终端查看一次性凭据，不发送到聊天或工单
cat deployment/secrets/initial-admin.txt
./scripts/compose.sh ps
python3 scripts/verify.py
```

首次管理员登录必须改密。初始化只作用于空账户库，重启不重置现有账号。使用 `./scripts/compose.sh` 管理服务，它隔离其他项目导出的环境变量；不要共享 `compose config` 的完整输出。

固定角色：`SYSTEM_ADMIN` 管理账号/授权并查看全部工位；`TEST_EXECUTOR` 在授权 Campaign 内执行测试业务，并可登录 HMI；`VIEWER` 读取获批数据，不能上报 HMI 状态。管理员在管理工作台创建执行员，执行员先通过网页登录完成首次改密。

## 4. 安装上位机

到 [v2.0.0 下载页](https://github.com/sss836/rp1_test_shn/releases/tag/v2.0.0) 下载
`rp1-test-hmi_2.0.0+ubuntu24.04_amd64.deb` 与同名 `.sha256`，放在同一目录。
先在现场完成正常停止并关闭自己的上位机，再安装：

```bash
sha256sum -c rp1-test-hmi_2.0.0+ubuntu24.04_amd64.deb.sha256
sudo apt install -o Dpkg::Options::=--force-confold ./rp1-test-hmi_2.0.0+ubuntu24.04_amd64.deb
```

应用菜单名称为 **rp1-test-hmi**，也可运行 `rp1-test-hmi`。
包包含 Python 3.12 / Qt 运行库和按当前 C++ 源码构建的电机 SDK，目标 Ubuntu 24.04 amd64。
本版由用户启动自己的 GUI 和网关，不安装自动启动服务，不配置免密提权。

```bash
rp1-test-hmi status
rp1-test-hmi stop
```

配置首次复制到 `~/.local/state/rp1-test-hmi/config/`，之后保留用户配置；
日志与本地记录分别在该状态目录的 `logs/` 和 `data/`。
系统 CAN 策略在 `/etc/rp1-test-hmi/can-policy.yaml`，配置接口需要管理员认证。
模块 YAML 使用相对引用，例如 `left_arm_motors.yaml`；网关仍检查配置根目录边界。

本版 PLC 模板为 `modbus_tcp`、`access_mode: control`、`192.168.137.10:502`、Unit ID 1、RP1 V1.0。
投用前核对本站实际设备参数及 [协议](hmi/docs/HOST_MODBUS_TCP_PROTOCOL_V1.0.md)，
在用户配置中设置。控制模式连接包含控制心跳及全部输出关闭的安全初始化；
需按现场操作流程进行。仅查看状态可使用 `access_mode: monitor`，离线验证使用
`--offline-check` 专用 mock 配置，不连接实际 PLC/CAN。

默认平台为同机 `https://localhost:8443`、协议 `reliability_v1`。
用户配置 `config/platform-ca.crt` 应为该站点实际 CA 公共证书，不能复用其它站点的证书。
执行员先通过网页登录完成首次改密，再在“老化测试”页登录平台；
只需在线监控时保留“未选择（仅本地保存）”上下文。
原始采集 CSV 保留在工位本地，监控状态不自动成为正式统计。

详细说明见 [当前上位机构建说明](hmi/docs/LOCAL_CONTROL_BUILD.md)。

## 5. 构建与维护

自行构建 HMI：

```bash
sudo bash scripts/install-build-deps-ubuntu24.sh
./scripts/build-hmi.sh
```

产物在 `hmi/dist/`；构建脚本固定 Ubuntu 24.04 amd64、独立虚拟环境和便携 CPU 编译设置。源码保留第三方许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

升级前备份：

```bash
python3 scripts/backup.py --maintenance
```

平台备份包含 PostgreSQL 与站点密钥，不包含工位本地 CSV；工位用户状态目录 `~/.local/state/rp1-test-hmi/` 和 CAN 策略 `/etc/rp1-test-hmi/can-policy.yaml` 须另行备份。恢复只面向新的克隆和新的 Compose 项目，详细步骤见 [运维](docs/OPERATIONS.md)。不要执行 `down -v` 清空生产数据。

本版本的自动化、隔离联调、已知限制与现场验收事项见 [验收记录](docs/ACCEPTANCE.md)。
