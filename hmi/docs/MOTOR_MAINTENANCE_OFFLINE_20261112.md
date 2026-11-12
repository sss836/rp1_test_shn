# 电机固件与参数本机维护

在 PLC 上位机顶部打开「电机版本 / 固件下载」。完成电机扫描后，可以按当前电机查询已缓存的本机固件，或导入签名固件包。平台查询与下载仍可通过测试平台账号使用。

## 准备本机资料

- 固件目录包含带签名的 `release.json` 和清单指定的 `.lsepbin`。如果此工位尚无 OTA 兼容映射，把管理员签名的 `motor-ota-policy.json` 放在同一目录，选择「导入签名固件」并打开 `release.json`。上位机按已扫描的型号、硬件、协议和软件版本匹配，检查签名、长度和 SHA256，再写入本机缓存。管理员已配置专用 OTA 映射路径时，由该路径管理映射。
- 参数目录由管理员将已发布模板导出并签名。进入「参数读取与维护」，选择「本机」，点击「导入签名模板」导入 JSON。目录中的型号和版本适用条件必须匹配实际电机。原始参数值和来源哈希随模板保留。
- 两种资料使用工位已部署的可信电机 Ed25519 公钥。只需要把签名资料带到工位，签名私钥留在管理员签名环境。

参数签名工具接受已发布模板记录数组，也支持带 `templates` 字段的平台目录导出。签名操作在管理员环境运行。

```bash
python3 hmi/tools/prepare_local_motor_parameter_catalog.py \
  --metadata published-templates.json \
  --private-key /secure/admin-motor-signing-key.pem \
  --output local-motor-parameters.signed.json
```

## 操作流程

1. 在固件或参数窗口把来源设为「本机」。未登录时默认使用本机。
2. 停测并确认电机失能。固件选择兼容版本。参数先使用「读取并比较」，查看实际值、模板目标和判定。
3. 单选或全选，点击「更新所选」，核对电机清单后确认。参数按所选电机的核准模板写入。
4. 按界面提示完成重新上下电及独立回读。维护记录保存在本机。写入异常或工位控制权失效时，记录保留实际完成状态，禁止自动重放。

在固件维护窗口点击「刷新版本」会发起一轮新的本机读取，逐台查询完整固件字符串 `0x31` 和数值软件、硬件、协议版本 `0x1E`。操作使用已经扫描的工位和现有版本读取线程。界面显示进度，完成后逐台显示结果或超时。刷新期间等待读取完成再使能或开始维护。

## 仅返回 `0.1.8` 的已核实 108 电机

左臂 `can0` 1–7 号的核实记录绑定有效工位配置、型号、CAN ID 以及数值软件 `0.1.8`、硬件 `0.1.0`、协议 `0.0.1.17`。实时读回一致时，页面显示「108（本地核实）」并支持单选和全选。`0x31` 超时保留在读回明细，原始固件字符串保留实际缺失状态。

- **普通参数**支持本机只读检查。写入使用已签名的 V1 普通参数模板，适用条目中的 `raw_firmware` 填代际标识 `108`，`software`、`hardware`、`protocol` 和型号必须精确匹配。软件在读取、写入前和重新上电回读时重新核对 `0x1E`。该适配用于普通参数，Pn 仍由对应协议和正式模板管理。
- **固件**使用已签名的 109 发布清单，`from_versions` 明确包含 `0.1.8`。签名 OTA 映射 `rp1-lro-ota-compatibility-v3` 对这类来源使用 `source_identity: numeric_legacy_108` 和精确软件、硬件、协议、型号，保留来源证据哈希以及完整 109 目标身份和固件 SHA256。写入前独立重读数值身份，写后核对完整 109 版本和协议。V3 仍可包含原始版本明确的 V2 条目。

## 代码与边界

- `factory_hmi/desktop/motor_firmware_dialog.py` 负责本机固件查询和文件导入入口。`motor_maintenance_dialog.py` 与 `motor_parameters_dialog.py` 负责来源选择、预览及人工确认。
- `factory_hmi/gateway/app.py` 使用启动时生成的本机桌面凭据和有效工位租约授权离线设备操作。预览、任务和回读绑定同一维护来源和操作会话。
- `factory_hmi/gateway/motor_firmware.py` 与 `motor_maintenance.py` 负责固件缓存、验签和 OTA 映射核对。`factory_hmi/gateway/motor_parameters.py` 与 `factory_hmi/motor_parameter_catalog.py` 负责模板签名目录的导入、匹配和重校验。
- `factory_hmi/core/motor_maintenance.py` 与 `motor_parameters.py` 执行设备就绪、独立身份读取和写后回读校验。仅数值回读使用明确核实的本机身份以及适用的签名资料。
- `factory_hmi/core/motor_versions.py` 对手动请求生成读取任务，复用同一线程合并重复点击，并通过网关和工位进程返回进度。断开连接会关闭读取线程并结束当前任务。
