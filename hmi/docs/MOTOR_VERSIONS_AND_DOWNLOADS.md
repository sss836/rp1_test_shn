# 电机版本识别与双更新下载（阶段2）

## 功能和边界

扫描电机打开既有驱动连接后自动查询LRO CAN-FD版本，读取本身不使能、不复位、不发送运动指令。每轮依次查询0x31原始字符串和0x1E数值版本，保留两份原始响应及错误。0x1E提供软件、硬件和可选协议版本；不把108自动映射成1.0.8，不从文件名推断型号或版本。原始字符串与数值软件版本不同记为unverified，不能以此推断完整兼容关系。

每个已连接后端只有一个可取消后台线程，单命令350ms预算，两命令逐台读取，每轮结束60秒后刷新。每个成功观测包含接口、Motor ID、关节、型号、UTC读取时间；180秒未刷新或连接关闭标记stale。超时、未知、不支持分别显示。版本响应按接口现有驱动对象、标准ID、查询类型和当前命令关联，500ms驱动接收窗口内接收；该协议无请求序号，无法从线协议证明同命令迟到帧的代次，真实总线仍需验收。

0x31原始字符串允许独立成功；0x1E超时时硬件/协议明确未知。下载兼容筛选要求实际版本状态known，型号、硬件、协议及当前完整版本均已知。缺项时拒绝选择，不使用手填值覆盖实际观测。

桌面顶部“检查更新”继续查询并下载DEB；新增“电机版本 / 固件下载”独立窗口。下载与实际版本分开显示；本阶段没有CAN刷写和参数写入动作。

## 分发契约与权限

DEB保持 `/api/v1/hmi/updates`、`rp1-hmi-release-v1`、`.deb` 和独立软件缓存。

电机固件使用 `/api/v1/hmi/motor-firmware`、`rp1-motor-firmware-v1`、`artifact_type=motor_firmware` 和 `.lsepbin`。清单包含完整目标版本、软件数值版本、LRO型号、硬件版本集合、协议版本集合、允许的当前完整版本集合、来源URL、范围、说明、长度和SHA256。全部元数据经Ed25519签名，服务端和客户端均验证；客户端使用管理员预先配置的可信公钥，不能从下载响应自行建立信任。

- POST `/releases`：仅有效HUMAN会话中的SYSTEM_ADMIN发布。原始固件作为application/octet-stream请求体；X-Motor-Release为签名JSON的base64编码。
- GET `/releases?model=...&hardware=...&protocol=...&current_version=...`：仅有效HUMAN执行员或管理员查询；按显式兼容条件筛选，软件版本按数字排序。
- GET `/releases/{release_id}/package`：执行员或管理员下载，重新核对同样的兼容条件。文件从已打开且校验过的描述符发送，拒绝持久副本损坏、路径穿越和符号链接。

发布先暂存，验证完整长度、SHA256、签名和文件类型，再在跨进程文件锁下原子更名。相同发布清单和内容重试幂等；相同型号/完整版本的兼容元数据或文件变化会冲突，不能覆盖。发布记录包含发布人、request_id和接收时间。上传不完整不会形成可查询发布。二进制内容仍为不透明厂商格式：后缀和非DEB检查不代表已经验证厂商固件内部格式或刷写安全性。

电机和DEB目录必须独立，均为仓库外绝对路径，不能与测量归档目录重叠；发布和下载不使用旧ingestion或数据归档接口。此实现的不可变发布清单/发布回执保存在分发持久目录，无新增生产数据库迁移。

## 配置与管理员操作

后端默认关闭电机固件分发。设置 MOTOR_FIRMWARE_ENABLED=true、MOTOR_FIRMWARE_ROOT（例如/var/lib/rp1-motor-firmware）、MOTOR_FIRMWARE_PUBLIC_KEY_FILE。大小和清单上限沿用hmi_updates_max_package_bytes（默认512MiB）及hmi_updates_max_manifest_bytes（默认16KiB）。必须给反向代理设置足够的请求体上限和发布超时；示例见deployment/motor-firmware.example.yml，尚未部署。

上位机网关通过 RP1_MOTOR_FIRMWARE_PUBLIC_KEY 指定可信公钥，原 RP1_HMI_UPDATE_PUBLIC_KEY 仍只供DEB使用。不要将私钥或会话凭据写入仓库、清单或报告。管理员根据厂商证据填写metadata JSON的version、software_version、vendor=LRO、model、interface_type=canfd、hardware_versions、protocol_versions、from_versions、source_url、scope、notes。

生成签名清单（不发布）：

    python3 hmi/tools/prepare_motor_firmware.py --firmware <固件.lsepbin> --metadata <已核实元数据.json> --private-key <仓库外私钥> --output <新清单.json>

管理员通过上述POST接口显式发布。现有发布角色控制由FastAPI登录体系确定，Pi/Agent不接管权限。未提供真实厂商文件或兼容证据时不能发布测试合成样本供现场使用。

## 客户端缓存与恢复

本机 `data_root/motor-firmware/releases/<release_id>/` 保存固件、签名清单和download.json；DEB继续使用 `data_root/software-updates/`，回执标记hmi_deb。电机回执标记motor_firmware，包含目标观测、来源、完整版本、哈希和下载时间，installed=false和actual_version_verified=false。下载不改变设备实际版本。

每次点击下载都重新查询当前账号权限和适用目录。网关从当前窗口服务快照取目标，拒绝未知/过期观测。网络请求使用独立会话cookie快照，禁止重定向；不占用平台心跳锁或控制锁。客户端在私有缓存锁下操作，完成前保留.partial暂存目录；每次读取有网络超时，总复制预算300秒，完成后校验哈希并原子更名。断流、长度或哈希失败删除本次暂存，旧成功文件保持；下次显式点击从头重试（不承诺断点续传）。损坏的已完成缓存隔离为.corrupt-*并重新下载；同一ID清单冲突拒绝覆盖。进程异常遗留.partial在下一次持锁下载时清理。没有固件刷写重试。

界面任务使用既有RestWorker；finished清理pending状态，关闭窗口或账号代次变化后丢弃旧回调。关闭/切换账号清空旧目录与回执显示。下载完成后仍需按后续阶段显式维护流程刷写，实际版本回读独立记录。

## 验证与交接

本阶段软件检查使用合成固件、临时Ed25519测试密钥、mock账号和原生离线CAN-FD传输，不发送真实CAN/PLC指令。原生测试验证错ID、扩展帧、错命令、超时迟到响应被拒绝，查询不影响遥测计数，不发送使能。真实0x31/0x1E设备格式、108与完整版本的对应、真实型号硬件兼容、分发站点和生产账号均待现场核对。

安装包的构建、安装和运行生效是三件事。不得为更新强杀控制进程或删除锁；任何安装须遵守AGENTS.md中的停止测试、实际失能、PLC总停止、正常退出及同状态目录status核对流程。当前阶段不提供安全安装授权证明，不将软件模拟当作设备验收。

## 阶段2软件收尾：服务关闭与报告生命周期

空记录/空历史目录不再启动启动恢复线程；显式查询历史仍会正常生成空台账。存在采集CSV或历史manifest时继续后台恢复，关闭会等待其完成，不删除原始文件或跳过恢复。

FactoryService.close先按既有正常断开流程停止控制/归零和传输，再通过wait_for_reports等待启动恢复与采集结束后的报告任务。报告任务等待其捕获的playback测试结束及CSV关闭后再归档；disconnect不能代替CSV/报告完成确认。启动、配置、使能、手动控制和归零确认等资源创建动作与关闭串行；快照与心跳不持此生命周期锁。报告线程启动和登记在同一短锁下，join不持服务锁或证据锁；完成线程句柄在确认退出前保留，防止漏等。

报告等待默认预算5秒，不代表任意原生设备断开都有5秒硬超时。超时抛出异常、保留仍活跃任务和关闭状态，允许后续显式重试；不强杀线程、不删除目录或锁、不宣称已关闭。已有报告错误保留在report_errors。GatewayRuntime关闭会调用服务close，GatewayHub会继续关闭其他窗口和资源并汇总抛出失败，失败实例及接口归属保留供重试。服务可用with FactoryService(...)管理生命周期；对非空数据目录，调用方应先停止接收操作，再正常close成功后释放/清理其目录。

Outbox短连接的with语义补齐为提交或回滚后立即close，初始化PRAGMA失败也关闭连接；显式ATTACH导入仍按原先finally断开。避免数据库/WAL/SHM句柄依赖后续垃圾回收释放。此次未修改数据库事务内容、核实归档审批或原始CSV事实。

在隔离复现中，原归零测试目录清理时，后台factory-report-recovery在线程构造返回后新建.evidence.lock，导致Directory not empty；归零使用Fake后端，没有版本查询线程。该时序说明这次复现不是版本查询触发，但暴露出服务后台文件任务缺少关闭契约。修复前后记录与完整收尾检查见outputs/animal-office/5705108e3f78422ba29774f6fd0245fa/。
