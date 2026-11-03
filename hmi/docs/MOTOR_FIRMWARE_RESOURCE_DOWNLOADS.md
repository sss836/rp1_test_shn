# 电机固件资源库：管理员确认范围，仅下载

固件资源库独立于DEB更新和可刷写电机固件。管理员可以发布用户已确认适用范围的lsepbin文件；执行员及管理员登录后可查询、下载，无需先扫描或连接电机。下载不会启用电机、写CAN、安装或刷写。

## 标签、适用范围与实际版本

109资源的`version_label=5.0.0.109`只保留文件名文字。`models`列管理员确认的当前四种LRO型号，不自动涵盖未来型号。用户确认原话、证据哈希和来源随签名保存。`software_version/hardware_versions/protocol_versions/from_versions`必须显式为null；不会把四段标签截成三段或把未知字段当通配符。

资源契约固定`rp1-motor-firmware-resource-v1` / `motor_firmware_resource` / `download_only` / `flash_eligible=false`。原`rp1-motor-firmware-v1`严格OTA契约不变；资源清单或回执不能用于模块固件维护。以后有完整机器版本映射时，须另行发布严格OTA清单及策略，旧资源保持不变。

## 管理员发布

服务端配置由部署维护人员显式设置：`MOTOR_FIRMWARE_RESOURCES_ENABLED`、`MOTOR_FIRMWARE_RESOURCES_ROOT`、`MOTOR_FIRMWARE_RESOURCES_PUBLIC_KEY_FILE`。资源持久目录必须在仓库外，与DEB、OTA和采集归档目录不重叠。复用已核对的受信Ed25519签发路径，不从文件或页面自动接受新信任key。

`hmi/tools/prepare_motor_firmware_resource.py`只准备签名清单，不发布。metadata须提供vendor、models、version_label、version_label_source、compatibility_source、compatibility_statement、confirmation_evidence_sha256、source_kind、source、scope、notes；四项未知映射可以省略，工具会明确写null，若传入推断值则拒绝。文件名、长度、SHA256、发布时间及随机release_id由工具生成。

API前缀`/api/v1/hmi/motor-firmware-resources`：

- `POST /releases`使用有效管理员真人会话、CSRF、`Content-Type: application/octet-stream`和`X-Motor-Resource`（签名封装JSON的base64），请求体为原始lsepbin。
- `GET /releases`执行员及管理员查询，可选`model`枚举过滤。
- `GET /releases/{id}/package`执行员及管理员下载，响应标明资源类型、SHA256及`X-Flash-Eligible: false`。

发布原子保存文件、清单和发布人/请求号/接收时间。同一ID或同目标标签冲突拒绝覆盖，只有整个原清单相同的重试幂等。文件大小/哈希/签名和类型检查失败不会成为可查询发布；原DEB接口保留。109文件约0.5MiB可通过现有nginx通用上传限制，更大资源还需核对站点代理上限。

## 上位机使用及恢复

“电机实际版本与固件下载”窗口新增“固件资源库下载（仅保存文件）”。窗口按当前账号查询目录和下载，显示确认范围、标签来源、未知映射及原始回执；没有电机版本也能下载，不能从此窗口开始OTA。

HMI使用管理员事先部署的`RP1_MOTOR_FIRMWARE_PUBLIC_KEY`。launcher仅在状态目录`config/motor-firmware-public-key.pem`确实存在时补充该环境变量，不获取新key，不覆盖明确环境配置。下载验签后保存到状态数据目录`motor-firmware-resources/releases/<id>/`，不写入原OTA缓存。

下载回执记录账号、缓存命中、来源清单、文件路径及时间，明确`installed=false`、`actual_version_verified=false`。失败重下清理本次残缺目录，损坏完成缓存另存`.corrupt-*`证据后重取；原已完成版本保留。账号变化停止当前操作并丢弃旧窗口回调；关闭窗口停止其定时器，下载不会创建控制租约或触发设备命令。
