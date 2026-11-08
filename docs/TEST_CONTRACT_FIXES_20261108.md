# 既有测试合同与诊断夹具修复记录

## 结果与范围

基于 `f67eed017d15f0489748ab097e1b79b4d6d82d6d`，修复前次记录的 2 项失败和 4 项夹具错误。最终全量后端非 integration 检查 **644 passed、276 deselected、0 failed、0 errors**。四项诊断集成测试另外在真实临时 PostgreSQL、API、Web、Pi 与合成模型栈中 **4 passed、0 skipped、0 failed、0 errors**，不是仅加标记排除。

仅修改测试、隔离运行器、CI 与说明文档，没有修改 API、HMI、Pi、前端、数据库或正式部署配置的产品源码。线上继续运行 `3.0.22-whole-robot1`；本轮不重建或重启生产实例，不安装上位机、不启动真实 PLC/CAN/机器人。

## 两项失败的原因与修复

| 文件与函数 | 原因 | 修复后的检查 |
| --- | --- | --- |
| `backend/tests/test_assistant.py` / `test_same_origin_entry_reuses_cookie_csrf_and_rejects_identity_headers` | 默认旧 probe 已关闭，旧断言仍期待 `assistant_not_configured`。 | 明确断言 HTTP 410 和 `assistant_legacy_closed`，保留匿名、CSRF、Bearer 和伪造用户身份原有保护检查；没有重新开放旧接口。 |
| `backend/tests/test_hmi_bridge_http.py` / `test_hmi_http_login_screenshot_provenance_logout_and_size_limits` | 超限请求漏传 generation，身份检查先返回 401；测试仍使用旧 4MiB 边界，而现有诊断上传合同已为 32MiB。 | 缺失／过期 generation 返回 401，且不调用远端；有效 generation 与 MIME 下，32MiB 接受、32MiB+1 返回 413且不转发。保留登录、窗口绑定、退出和 `hub._started is False` 检查。 |

请求先经过本地操作员和窗口会话校验，再验证上传容量，最后才允许转发。原测试在前一步即被拒绝，不能据此判断容量保护失效。修复使两个阶段各有有效证据，没有放宽任何实际限制。

## 四项夹具错误的原因与修复

`backend/tests/test_assistant_diagnosis_integration.py` 仅导入 `signed_in`、`control`、`submit`、`wait` 等普通函数；pytest 不会据此自动发现其他模块的 `environment`，也不会传播 `normal_model` 和 `clear_finance` 的 autouse 注册。

现在显式引入原有三项夹具，并将该模块标为 integration／独立合成栈检查。普通单元检查不需要连接模型或临时数据库；专用运行器必须实际执行以下四个原测试：

1. 网页批准外部临时文件读取后，原 HMI 桥接读取原文件并保留证据哈希、间隙及任务收据；网页本身不能冒充本地读取。
2. 上传完整 CSV 后，在无 HMI 条件下分析冻结证据，包含尾部异常、完整行数和缺口。
3. 两项等待本地读取权限的任务不占满两个执行线程，其他普通查询仍能完成；等待期间无未结数据库事务。
4. 原 HMI 自动读取已有 CSV 尾部和未上传日志，沿原任务恢复，验证来源哈希、读取次数、轮次和恢复状态，原始文件内容不变。

## 可复现运行器与 CI

`scripts/test-assistant-isolation.py --diagnosis-checks` 新模式：

- 使用随机 Compose 项目、内部网络、tmpfs 数据库和临时归档；不读取现场 `.env`，不使用生产数据库、归档或模型密钥。
- 只运行这四个诊断测试，开启临时 runtime、关闭 legacy probe；只给 checks 容器挂载 HMI 源码只读目录，不运行上位机 lifespan。
- 价格到期保护原来会随时间使合成夹具失效。本模式仅为临时 API／checks 生成未来一天有效的合成价格期限，不修改站点价格或产品到期保护。
- 新镜像使用本次随机标签，生成的路径、凭据及项目配置优先于宿主导出的同名变量，并清理继承的 `COMPOSE_*` 选择参数；显式 QA 镜像选择仍保留。
- 拒绝与真实模型密钥、production、观察或其他检查模式混用；解析 JUnit，必须恰好四项且无 skip／failure／error，才写通过记录。
- finally 收集脱敏日志并清理该随机项目的容器、网络与卷。

`backend/tests/test_assistant_isolation_runner.py` 新增三项无 Docker 回归：宿主变量不能替换临时路径；即使进程返回 0也拒绝跳过用例的“通过”；真实密钥组合在任何 Docker 操作前拒绝。

`.github/workflows/ci.yml` 增加独立 `assistant-diagnosis` job，执行上述模式并保存检查报告。原普通 integration glob 不包含诊断模块，因此单独接入 CI，避免只排除原四项而丢失覆盖。

复现命令：

```bash
python -m pytest -c backend/pytest.ini backend/tests -m 'not integration' -q
python3 scripts/test-assistant-isolation.py --diagnosis-checks --report-dir /tmp/rp1-assistant-diagnosis-report
python3 scripts/test-archive-deployment-guards.py
python3 scripts/check-release.py
```

## 实际验证与运行边界

- 全量后端单元：644项通过，276项 integration 单独收集；保留两个原有依赖警告。
- 两个原失败文件：7项通过，包含 ASGI 原 HMI 登录／上传／退出；lifespan 未启动。
- 默认从当前 requirements 重新构建 QA 镜像、重新构建独立 Pi 镜像后，四项诊断全部通过；Pi Node 测试40项通过。
- 运行器保护3项、既有部署清理保护6项、Git 文件卫生与文档链接检查通过。
- 独立确认 Pi 无法解析 PostgreSQL、不挂 Docker socket／归档；检查后随机项目的容器、网络、卷全部清零。
- 独立核对原7个生产容器的 ID、镜像、启动时间、健康及所有挂载来源摘要，与本轮开始完全相同。

首次手动尝试指定了不包含 Alembic 的 API 派生 QA 镜像作为 migrate，启动在临时迁移阶段被拒，临时栈完整清理。随后使用 CI 同样的默认全新依赖镜像构建路径通过；没有修改产品依赖来掩盖该运行配置错误。

本机脱敏证据在 Git 忽略的 `outputs/test-contract-fixes-20261108/`：`backend-unit-all-final.log`、`unit-contracts.log`、`diagnosis-verification.json`、`diagnosis-stack-default/backend-junit.xml`、`diagnosis-stack-default/backend-tests.txt`、`diagnosis-stack-default/node-tests.txt`、`diagnosis-runner-guards.log`、`independent-test-fix-audit.json`。没有把临时数据库、凭据、模型密钥或运行原始数据提交 Git。

以上为软件与合成故障验证，不代表真实设备联调或长期稳定性测试。
