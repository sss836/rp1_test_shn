# RP1 助手运行与恢复

本版使用大屏同源 FastAPI → 独立 Pi → 固定业务工具 → FastAPI。模型固定为 DeepSeek 官方 `deepseek-flash`，Pi 的依赖版本由 `assistant/package-lock.json` 锁定。Pi 不连接数据库、归档目录或设备。

## 任务与真实结果

- `backend/app/services/assistant_chat.py::submit` 在短事务内锁定会话、保存用户消息ID，固定当时可见的历史、当前指令依据、对象匹配结果和页面上下文，计算输入哈希后入队。模型等待期间不持有事务。
- `run` 只使用持久化快照。新的明确创建指令不继承另一个创建指令的参数；补充字段可继续使用最近指令。`setup_assistant.py::commit_intent` 再核对输入哈希、原账号会话、权限、任务截止及字段来源。后发消息不能给先发任务补参数、否定或授权发布。
- `database/sql/050_assistant_inputs.sql` 追加快照、边界和哈希，禁止改写已冻结输入。升级前无快照任务中断而不重放。快照正文与聊天同样30天清理；业务回执、必要哈希和财务审计独立保留。
- 项目对应 `program`，测试计划对应 `campaign`。字段齐全且指令明确时直接调用工具；缺项、歧义或建议参数才追问。`COMPLETED` 只表示对话结束，前端另显示“待补充 / 待采用 / 已取得保存回执”。只有真实回执才能证明已保存。
- 断流、取消、退出、账号停用及超时后仍按原请求键核对业务回执。已提交对象不自动撤销；请勿换请求键盲目重试。

## 限额与时间

站点每日100元，北京时间零点切换记账日，最多两个任务执行，其他任务排队。单任务执行至多120秒、至多6次模型请求。每次实际发送前预留保守费用，用实际Token回执结算。缺少可靠用量则保留预留并冻结站点新增模型调用，跨零点及重启均不自动解除冻结。普通业务不依赖助手可用性。

`assistant/src/chat.mjs::modelTimeout` 将剩余时长限定为正整数毫秒：SDK拒绝带小数的超时参数；该问题曾使四次25秒请求后的第五次调用在发出前失败。总120秒限制保持不变。日志仅记录固定阶段、轮次、耗时和HTTP状态，禁止记录提示词、原始模型异常和密钥。

当前CNY费率取已核验官方高峰价格作为费用上界，不是供应商账单；来源：[DeepSeek官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。核验有效期到达后停止新调用，需重新核对并更新服务端配置。

## 本机部署

`deployment/compose.assistant.yml` 是部署模板。仅当本机存在被Git忽略的 `deployment/assistant-site.override.yml` 时，`scripts/compose.sh` 才加载助手配置；原 `.env`、`compose.override.yml`、端口、TLS与持久卷继续沿用。站点覆盖文件固定实际镜像摘要、服务端秘密文件路径、指定账号UUID和启用开关。

正式模式保持 `APP_ENV=production`、session认证和Secure Cookie。开放时需要同时开启 `ASSISTANT_ENABLED`、`ASSISTANT_RUNTIME_ENABLED`、`ASSISTANT_PRODUCTION_ENABLED`，配置非空试点名单，且 `ASSISTANT_LEGACY_PROBES=false`。API明确单worker；Pi只接工具内网及模型出站网，不加入数据库所在网络，没有宿主端口。外部Nginx屏蔽 `/api/v1/assistant/tools/`。

部署前保留旧API/Web镜像与站点覆盖文件，并在仓库外保存数据库备份。先执行追加迁移，再仅替换API/Web及新增Pi；不重建PostgreSQL或工作进程，不操作PLC/CAN。部署后核对实际容器镜像、进程启动时间、数据库迁移版本、健康端点、登录权限和助手真实回执。具体本机路径、摘要及检查结果写入对应任务交付目录。

恢复时先关闭助手入口，正常停止新增Pi任务，恢复旧API/Web镜像及覆盖文件。数据库迁移不降级、不删表，不自动恢复旧备份覆盖上线后业务，不清除未知费用冻结或已有业务回执。需要核对供应商用量后，通过受审计的运维流程处理账务。

## 隔离检查与观察

短时检查使用 `scripts/test-assistant-isolation.py`：`--chat-checks` 检查任务运行；`--setup-checks` 检查统一建档；`--production-checks --deepseek-key-file <服务端文件>` 在一次性数据库中检查HTTPS生产形态。密钥值不进入命令和报告。每次运行使用独立Compose项目并清理自己的容器和tmpfs数据库。

连续观察使用 `--observe-seconds 86400 --report-dir <新目录>`，仅允许合成模型和指定一次性数据库。建议通过用户systemd单元启动以脱离对话进程；SIGTERM触发隔离栈清理。每分钟保存真实时间、三项查询任务、费用、日界线、并发上限及普通业务探针，定期重新登录，检测后端源码变化。`observation-status.json`、`observation-samples.jsonl` 保留连续证据；只有实际持续86400秒且 `full_24h=true` 才表示24小时完成。60秒自检、后台启动、原站点在线时长均不等于完成。临时数据库不保存真实业务或设备数据。

## 合成界面预览

`scripts/assistant-preview.sh build` 构建真实助手组件的合成预览；`start / status / stop` 通过用户systemd单元维护进程。地址为 `http://127.0.0.1:5179/`，仅监听本机；页面明确标注合成数据，不连接真实模型、认证或数据库。实际网站功能以8443站点及真实接口回执为准。
