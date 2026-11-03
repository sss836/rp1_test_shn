# 独立 HMI 更新器：软件实施与维护边界

**2026-11-03：已登录HTTPS平台通道更新不再需要客户端更新公钥。** 服务端发布继续验签；客户端保留HTTPS登录、系统/版本、大小、SHA256与DEB核验。离线或任意URL更新仍按原规则验签。旧版需先安装完整新版，不能只上传后继续运行旧代码。

2026-10-28 本机部署状态已更新，见 [OTA_DEPLOYMENT_20261028.md](OTA_DEPLOYMENT_20261028.md)。本机平台 OTA 服务和公钥已部署，2.0.16 签名包已由管理员通过正式接口发布，真实 HTTPS 下载及验签通过；下文“本轮未部署”描述早期软件实施记录。完整自动安装的既有限制仍保留。

2.0.17 起按老板最新要求，显式请求安装后重启时沿用保存的正式 PLC 配置，不再自动追加 `--update-monitor`。启动不会自动使能电机或运行测试；仍检查新实例版本、进程身份及电机未使能状态。只读/模拟保留为人工明确选择的诊断入口，不作为正常安装后的默认方式。

独立安装事务已经实现，包含受信验包、依赖检查、短时安全证明、旧实例正常退出、同状态目录持锁安装、版本和新实例身份核对及失败留证。已用注入的模拟执行器走通完整成功与故障路径。本轮没有真实安装、部署生产公钥、启用发布服务或 PLC/CAN 操作。当前尚缺可信签发端及真实电机失能反馈，图形界面也尚未接通安装发起；不能声称工厂 OTA 已可执行。软件布尔值、用户可写 JSON 和 mock 数据均不能补足真实证明，预检或下载成功不等于安装授权。

## 文件与调用关系

| 文件 | 接口 | 行为 |
| --- | --- | --- |
| `factory_hmi/updates.py` | `detect_host`、`installed_version` | 区分已加载更新器代码版本与 dpkg 已安装版本；不把磁盘安装状态当成运行代码版本 |
| 同上 | `verify_release` | 精确检查清单格式、Ed25519 签名、Ubuntu 系统/架构和 Debian 版本升级关系 |
| 同上 | `stage_release` | 图形网关与终端共用；认证流或内网下载流 → 长度、SHA256、DEB 元数据 → 原子缓存 |
| 同上 | `install_preflight`、`protected_install` | 无证明时明确阻止；有受信签名证明时调用独立预检与安装事务，不再永久拒绝 |
| `factory_hmi/installer.py` | `execute_install_transaction` | 固定 root 政策、受信发布/安全公钥、单次证明、旧实例退出、同 inode 启动锁、安装和留证 |
| 同上 | `verify_installation` | 人工安装并手动重启后，核对 dpkg、真实 `/proc` 进程身份、旧实例差异和网关 health |
| `launch.py` | `update`、`check-update`、SIGUSR1 | 更新命令提前分派；升级专用正常退出无 kill 后备；超时保留实际身份与锁 |
| `packaging/installer-helper` | 固定特权入口 | DEB 打包到 `/usr/lib/rp1-test-hmi/installer`，固定系统 Python 和已安装模块路径；不自动授予提权 |
| `tools/prepare_update_release.py` | 离线准备 | 使用维护人员已有 Ed25519 私钥签名，不生成、部署、复制私钥或发布软件 |

终端通过 `runpy` 载入独立模块，避免 `factory_hmi/__init__.py` 的 numpy/运动运行时依赖。图形界面通过网关的 `SoftwareUpdates` 调用相同验证与缓存函数。检查只查询，点击下载才缓存；下载从平台专用软件通道取得数据，不使用 ingestion、附件或测试归档。

## 在线平台更新与离线更新的信任来源

2026-11-03 起，桌面上位机通过**已登录的HTTPS平台专用软件通道**检查和下载更新，不再要求每个用户单独部署 `config/update-public-key.pem`。程序只接受已配置的HTTPS平台根地址、真实账号会话以及固定的 `/api/v1/hmi/updates/latest` 和 `/releases/{id}/package` 接口；保持TLS证书验证、拒绝重定向，并在下载/发布缓存前复核账号身份。未登录、HTTP、匿名来源及随意URL不进入此路径。

平台服务端在管理员发布时继续验证Ed25519签名，只有通过发布校验的完整包才进入软件目录。客户端仍核对精确发布契约、系统/架构、版本递增、声明大小、SHA256和DEB元数据，流式写入私有临时目录并原子发布缓存；不会因为没有本地更新公钥而阻止已认证平台更新。这不是关闭TLS，也不会改变发布签名或安装/设备安全门禁。

**离线清单或任意URL备用源**继续使用原独立更新器，必须经管理员以受信方式部署Ed25519公钥，且继续完整签名验证。不能用在线平台缓存路径标记绕过离线验签。公钥须为非组写、非其他用户可写的PEM普通文件，可通过 `RP1_HMI_UPDATE_PUBLIC_KEY` 或终端 `--public-key` 显式选定。

旧版进程仍执行旧公钥规则，因此首次需通过已认证的平台网页取得完整新版并正常安装；仅在服务器上传新版不会改变旧进程。缓存目录默认为 `STATE/data/software-updates`，由当前用户持有且为0700；路径不得包含符号链接。上述更新不自动安装或启用任何设备。

## 终端检查与下载

下列为待维护人员按实际地址、状态目录和已部署公钥填写后使用的示例；本轮未连接这些来源。

```bash
rp1-test-hmi check-update --source "$LAN_SOURCE" --state-dir "$STATE"
rp1-test-hmi update check --source "$LAN_SOURCE" --state-dir "$STATE"
rp1-test-hmi update stage --source "$LAN_SOURCE" --state-dir "$STATE"
rp1-test-hmi update stage --release "$SIGNED_RELEASE" --package "$SIGNED_DEB" --state-dir "$STATE"
```

`RP1_HMI_UPDATE_SOURCE` 可提供备用源默认地址。源使用 `http(s)://主机[:端口]` 根地址和与平台一致的只读接口：

- `GET /api/v1/hmi/updates/latest?os_id=...&os_version=...&architecture=...&current_version=...`，返回 `data` envelope。
- `GET /api/v1/hmi/updates/releases/{release_id}/package`，返回 DEB 二进制。

发布服务未在本轮安装或启动。备用源可完全位于内网，不依赖 GitHub。所有重定向均被拒绝；URL 禁止内嵌用户名、密码及 fragment。HTTP 内网源同样必须通过签名、SHA256 和 DEB 校验。需要平台会话时由图形网关提供认证流；Python API 的 `headers` 只保存在调用期间内存，不记录 URL、token 或响应正文到缓存。终端不从其他程序读取或代管登录凭据。

清单 schema 为 `rp1-hmi-release-v1`，签名是对 `json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf8')` 产生的字节进行 Ed25519 签名后的 Base64。SHA256 与长度覆盖整个原始 DEB；Package、Version、Architecture 必须与清单一致。终端不会安装为了通过校验而改写版本号或包架构的文件。

缓存写入使用互斥锁、同文件系统临时目录、fsync 和 rename；重复发布编号对应不同清单会被拒绝。传输长度不超过已签名长度，总下载预算五分钟，单次 HTTP 阻塞读取受 15 秒超时约束。校验失败会清理临时目录，原索引、已缓存包与已安装版本不变。断电可能留下未被索引引用的完整发布目录，后续同一清单可重新核对；不会把半包选为当前版本。

成功返回 `status=staged`、`manifest`、`package_path`、`release_path`、`previous_package_path`。后者仅指前一次成功缓存包；首次缓存时为 null，不表示当前已安装版本的恢复包已经存在。需要恢复时须先确认确有完整旧包及其签名，并经过同样的安全停机和安装授权；程序不会自动降级、删除配置、运行记录或单实例锁。

## 安装门禁及核对

以下命令为另行取得安装授权并完成首次受信部署后的维护用法，本轮仅测试了完全隔离的模拟事务。

```bash
rp1-test-hmi update install-preflight --release "$CACHED_RELEASE" --state-dir "$STATE" \
  --proof "$TRUSTED_SAFETY_PROOF" --previous-release "$INSTALLED_VERSION_RELEASE"
pkexec /usr/lib/rp1-test-hmi/installer --release "$CACHED_RELEASE" --state-dir "$STATE" \
  --proof "$TRUSTED_SAFETY_PROOF" --previous-release "$INSTALLED_VERSION_RELEASE"
```

无证明的预检保留 `ready=false` 与原因；有证明的预检通过独立受信政策验证发布和安全签名、账号角色、系统架构、安装版本、旧实例实际 PID/start_ticks、全部窗口和 PLC 状态，并模拟依赖，不消费证明、不停止进程、不安装。`ready=true` 仅表示可继续请求退出和本机授权，并非安装完成。实际事务还会重新执行全部门禁，防止预检后状态变化。

真实 helper 只读取固定 `/etc/rp1-test-hmi/installer-policy.json`，不能通过 CLI 指定其他政策、脚本、Python、受信 key 或模拟执行器。政策 schema 为 `rp1-hmi-installer-policy-v1`，字段为 `state_root`、`spool_root`、`release_public_key`、`safety_public_key`、`launcher_path`、`python_path`、`run_uid`、`run_gid`，可选 `session_env`。所有工具与公钥路径必须为 root 持有、非组写/其他用户写、无符号链接的规范绝对路径；spool 也必须如此。状态目录与 run 目录必须属于明确的非 root 运行账号。首次部署由管理员依据真实账号、系统解释器、状态目录和会话配置完成，程序不会生成凭据、填写猜测 UID/GID 或自动部署该政策。

helper 源码是固定 shell wrapper，只执行 `/usr/bin/python3 -I -S /opt/rp1-test-hmi/hmi/factory_hmi/installer.py`。打包仅提供程序，不部署安全/发布公钥、生产私钥或允许免认证的 polkit 策略。图形端未来只能调用这个固定入口并遵守本机系统授权；平台 `SYSTEM_ADMIN`/`TEST_EXECUTOR` 不自动获得 root 权限。

事务先将目标包复制到受保护 spool 并重新验签、核对长度/SHA256/DEB 元数据，调用方随后改写原缓存不会影响安装字节。已有安装版本时必须同时提供对应版本的受信完整旧包，先复制留存后才可停止实例。依赖用 apt `--simulate --no-download --no-remove` 检查；如果还需安装、配置或移除其他包则阻止，不联网修复。

安全证明通过后在 spool 中单次消费 proof_id，再用 pidfd 绑定旧 launcher 并复核 `/proc` 身份后发送 SIGUSR1。启动器仅正常终止自身桌面/网关，不使用升级 kill 后备；子进程未退出时保留 launcher 身份与原启动锁。helper 等待超时即报告，不能删锁或强杀继续。确认两个旧进程均退出，同状态目录 status 为 `running=false`，并持有原 `run/launcher.lock` 后，最终重新验包、依赖和证明期限，再调用固定 dpkg 安装命令。

每次事务在受保护 spool 的 `transactions/<id>/` 下保存 `receipt.json`、`events.jsonl`、依赖/安装日志和目标/上一版包。失败保留实际已安装版本或未知状态，不自动降级、删配置/运行记录、重试启动或使能。系统安装子进程超时也不会被本工具强杀，报告 PID 和可能仍在运行的状态，须人工核查。签名证明一旦消费，失败重试也必须重新取得真实安全检查与新证明。

默认 `restart=false`，成功安装返回 `installed_pending_restart`、`verified=false`。显式传 `--restart` 才以原非 root 账号启动同状态目录的 `--update-monitor`：沿用原 PLC driver，临时设置 monitor、禁用 CAN/control，不改保存的配置，也不将 mock/offline 状态改为实机。须配置真实桌面会话环境，不猜测显示号。仅在 dpkg 版本、health 代码版本、实际新 PID/start_ticks、只读模式均一致后返回 `status=verified`，且始终 `hardware_validated=false`。

## 可信安全证明契约

证明对象必须恰好为 `{claims, signature}`。claims 的 schema 为 `rp1-hmi-safe-stop-v2`，字段与验证代码 `installer.PROOF_FIELDS` 一致：

- `proof_id` 是单次 32 位小写十六进制编号；绑定 `release_id`、`package_sha256`、绝对规范 `state_root`、当前 `boot_id`。
- `observed_at`、`expires_at` 带时区，期限最多 60 秒；未来/过期证明被拒绝，并在真正安装前最后一次检查。
- `launcher`、`gateway` 各为真实 `{pid, start_ticks}`；不能用创建网关的时间代替 OS 进程启动 ticks。
- `requester={subject,role}` 必须来自真实已授权账号；role 仅允许 `SYSTEM_ADMIN` 或 `TEST_EXECUTOR`。
- `all_windows_checked=true`，非空 `windows` 的 owner 唯一；每项必须精确为 `motion_active=false`、`motors_disabled_confirmed=true`、`feedback_fresh=true`。
- `plc` 必须是 `driver=modbus_tcp`、`communication_state=LIVE`、`stale=false`、`normal_all_stop_confirmed=true`、`feedback_fresh=true`、`main_ready=false`，且四个 `channel_permit` 均为精确 false。mock 或数值代替布尔值不被接受。
- `control_inhibited=true`，`quiescence_token=proof_id`。签发端必须先覆盖全部窗口并锁定后续控制写入，不能签完证明后继续允许运动。

signature 是对 claims canonical JSON 进行 Ed25519 签名的 Base64。安全公钥独立于发布公钥，由首次部署政策选定。`sign_safety_proof` 仅是可信签发 broker 的内部工具和测试工具，没有无验证 CLI 签名入口。签发私钥不能被桌面调用者读取；broker 不得盲签调用者上传的 JSON，必须自行取得真实设备反馈、账号授权、全部窗口和控制锁定依据。当前电机 SDK 缺实际失能确认，受保护可信签发端也尚未部署，因此实际生产签发应继续明确阻止。模拟测试中的测试密钥与 synthetic provider 不可作为现场证明。

## 人工安装后的独立核对

已获授权完成手动安装并手动启动新实例后，可运行：

```bash
rp1-test-hmi update verify --release "$CACHED_RELEASE" --state-dir "$STATE" --previous-identity "$PREVIOUS_IDENTITY_JSON"
```

更新前身份 JSON 为 `{ "pid": <真实网关PID>, "start_ticks": "<真实启动ticks>", "boot_id": "<真实boot_id>" }`，不能事后伪造。verify 必须同时确认 dpkg 已安装版本等于签名版本、同状态目录正在运行、实际 PID/start_ticks 与 health 一致、运行版本等于签名版本，且身份与更新前不同。通过仅表示软件版本和实例身份核对一致，返回 `hardware_validated=false`；不会自动使能或开始测试。软件启动检查不能替代真实设备验收。

## 离线签名准备

```bash
python3 hmi/tools/prepare_update_release.py "$BUILT_DEB" \
  --private-key "$EXISTING_OFFLINE_ED25519_KEY" \
  --os-id "$VERIFIED_TARGET_OS_ID" --os-version "$VERIFIED_TARGET_OS_VERSION" \
  --output "$NEW_RELEASE_JSON" --notes "$RELEASE_NOTES"
```

Version、Architecture、Package 从原 DEB 读取；目标系统字段须由维护人员明确填写。工具只写全新 release JSON，不覆盖既有输出、不产生生产私钥、不上传、不部署。目标 DEB 和签名清单交付须按仓库约定归档至 `hmi/dist/版本或批次/` 并保持 Git 忽略；生产签名钥匙始终在仓库外。

## 开发检查与未覆盖事项

`tests/test_software_updater.py` 用临时测试密钥及无 maintainer script 的惰性 DEB 验证签名、篡改、系统/架构、Debian 版本排序、限长、摘要、DEB 元数据、缓存权限、符号链接、失败恢复、上版保留、重定向、安装拒绝及新旧实例区分。测试只在临时目录构包和签名，不执行真实 stop 或安装；更新后的启动器已通过 `python3 -I -S ... update --help` 和 status 检查。

`tests/test_software_installer.py` 还覆盖受信签名的完整模拟事务、默认不重启、模拟监控重启与新实例核对、依赖/退出/锁/安装/半安装/health 失败、安装前过期、证明重放、错误身份/boot/包/目录/角色/PLC/窗口，以及 pidfd 顺序。模拟安装只修改 FakeExecutor 的内存版本，不调用真实 dpkg、stop、启动器或设备。

最新事务记录见 `outputs/animal-office/hmi-v1-completion/installer/`；原更新器记录在 `outputs/animal-office/hmi-v1-implementation/updater/`。可信生产签发端、SDK 实际失能依据、首次政策/公钥部署、图形安装发起、实际 OS 授权交互、真实安装和恢复、新实例生效及 PLC/CAN 均仍需完成或现场验证。未执行八小时长测，不设独立验收通过结论。
