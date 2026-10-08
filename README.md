# RP1 Test SHN — Reliability Platform 与 RP1 TEST HMI（PLC）

当前工程版本 **1.0.1（预发布）**，目标系统 **Ubuntu 24.04 LTS amd64**。包含当前可靠性平台的 React 前端、FastAPI 后端、PostgreSQL 数据库、MTBF Worker，以及 PLC/CAN 上位机完整源码和构建工具。仓库采用 `reliability_v1` 协议；不是旧版 dashboard 平台。

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
git checkout v1.0.1
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

到 [v1.0.1 下载页](https://github.com/sss836/rp1_test_shn/releases/tag/v1.0.1) 下载 deb 与同名 `.sha256`，放在同一目录。安装/升级会重启网关，须在测试结束后进行：

```bash
sha256sum -c rp1-test-hmi_1.0.1+ubuntu24.04_amd64.deb.sha256
sudo apt install -o Dpkg::Options::=--force-confold ./rp1-test-hmi_1.0.1+ubuntu24.04_amd64.deb
sudo usermod -aG rp1-factory "$USER"
```

注销并重新登录。包包含 Python/Qt 运行时和 motors 扩展，不需要在操作员工位安装开发环境。配置位于 `/etc/rp1-test-hmi`，CSV、记录与 outbox 位于 `/var/lib/rp1-test-hmi`，服务为 `rp1-test-gateway.service`，应用菜单名称 **RP1 Test HMI (PLC)**。

在平台源码目录配置工位，例如同机部署：

```bash
sudo python3 scripts/configure-hmi.py   --url https://localhost:8443   --station-id SHN-PLC-01 --bench-id RD-01   --ca deployment/secrets/root-ca.crt --outputs-off
```

分机时替换为实际服务器 HTTPS 地址及本机收到的 CA 文件路径。此脚本设置 `RP1_FACTORY_PLATFORM_MODE=reliability_v1`，不索取旧平台 machine token。在线监控只需真实平台账号，不要求服务 API key。

打开 HMI，在“老化测试”中的平台登录区域输入执行员账号密码。看到“监控在线”后，在网页打开 **工位监控**。监控每 10 秒上报、页面每 5 秒刷新、45 秒无心跳离线；管理员看全部工位，执行员看自己登录的工位。控制器采样或网页响应过期时显示未知/历史状态，不把历史运行信息当作当前状态。并行模块分别显示测试单号、样品、状态、时长和循环。

只需监控时，测试上下文选择“未选择（仅本地保存）”；不要为监控填写虚构上传 UUID。HMI 提供“退出平台”按钮。关闭窗口与退出账号不同，网关服务可能继续上报；会话默认 8 小时，过期后重新登录。原始 CSV 始终由本地采集/记录流程保存，状态监控不将其写入正式统计。

默认 PLC 配置为 mock，页面会明确标记模拟。真实硬件投用必须按 [PLC 接口映射](hmi/docs/PLC_INTERFACE_MAPPING.md)、[协议](hmi/docs/HOST_MODBUS_TCP_PROTOCOL_V1.0.md) 和现场验收确认；不能用模拟结果代替真实设备验收。

已有本机开发平台若使用 HTTP 8088，需手动设置对应地址，详见 [上位机接入](hmi/docs/PLATFORM-PRESENCE.md)。这与本仓库全新部署的 HTTPS 8443 是两种配置，不要混用。

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

平台备份包含 PostgreSQL 与站点密钥，不包含工位本地 CSV；工位 `/etc/rp1-test-hmi` 和 `/var/lib/rp1-test-hmi` 须另行备份。恢复只面向新的克隆和新的 Compose 项目，详细步骤见 [运维](docs/OPERATIONS.md)。不要执行 `down -v` 清空生产数据。

本版本的自动化、隔离联调、已知限制与现场验收事项见 [验收记录](docs/ACCEPTANCE.md)。
