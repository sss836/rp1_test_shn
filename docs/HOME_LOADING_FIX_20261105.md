# 首页长时间停留加载骨架：定位与修复

日期：2026-11-05，时间说明使用北京时间。修复分支 `codex/fix-home-loading-20261105`，独立工作区 `/home/shn/.codex/worktrees/rp1-home-loading-20261105`，基于生产源码 `35cd7e70312f760a4c222475a4ea67d784c4ba71`。本轮不推送 GitHub，不合并整机工厂适配候选。

## 现场证据与原因

用户截图显示导航已加载，正文为三块统一骨架。该形状对应 `Workbench.LoadingState`；生产基线 `HomePage` 等待 `/api/v1/dashboard/home` 时会进入该分支。首页静态导入，不需要另外下载一个首页懒加载模块。

脱敏访问日志中，目标 Chrome 于 16:32:32 登录及 `auth/me` 成功，到 16:33:18 才记录首页接口成功；两者相隔46秒。随后首页成功记录间隔为74、73、74秒，多个 Firefox 客户端同样约72–73秒，和30秒轮询加较慢处理相符。原访问日志没有 request_time，不能将这些时点差当作精确HTTP耗时。所查窗口未见 JS/CSS 4xx/5xx、502/503/504；登录前401和其他上位机401不作为此次首页慢查询的原因。

同时捕获 PostgreSQL 多条首页时长汇总 SELECT 处于 active、无锁等待，运行已达31秒以上，CPU采样约198%。表统计当时约175条执行、23条时长记录（n_live_tup估计值），不支持“波形文件太大／内存不足”的直接结论。

调用链为：`HomePage` → `getDashboardHome()` → `/dashboard/home` → `ReadModelRepository.dashboard_home()` → freshness、来源、异常、范围和用例时长汇总。主要延迟落在最后的 `case_rows` 查询：它把时长资格结果及执行汇总连接到用例／样品目录，未物化的中间结果存在重复资格计算路径；所涉及分区、权限和暴露资格函数本身又包含嵌套查询。

只读对照使用同一现场数据库和有效管理员上下文，原始测量及业务表均未写入：

| 对照 | 结果 |
| --- | --- |
| 原SQL，仅关闭本事务JIT | 最后用例汇总仍超过7秒，触发设定的语句超时 |
| 仅物化首页ledger和execution_ledger，JIT关闭 | 完整首页0.6318秒，用例汇总0.3570秒 |
| 相同物化，JIT保持开启 | 完整首页0.7164秒，用例汇总0.3978秒 |
| 修复上线后，真实运行代码与原JIT配置，连续三次只读 | 0.6242／0.6559／0.6508秒，均返回2组范围、70个用例 |

因此没有全库关闭JIT，也没有修改审批、分区或暴露资格函数。改动只固定首页的中间汇总，避免重复计算。不将未采集的每个SQL函数调用次数或精确重复倍数写作已确认事实。

原前端 `fetch` 和读取响应正文都没有主动截止时间；React Query默认会重试一次。如果请求迟迟不结束，页面会持续显示骨架。这是延迟在界面上被放大的第二层原因。

## 文件级修复

| 文件 | 修改与行为 |
| --- | --- |
| `backend/app/repositories/read_models.py` | 新增首页专用 `HOME_LEDGER_CTE`，仅首页把ledger和execution_ledger标为MATERIALIZED；共享 `LEDGER_CTE` 及其他读模型不变。 |
| `backend/app/services/dashboard_queries.py` | 新增 `dashboard_home_deadline()`，为该次首页的全部SQL共享8秒预算。每条执行前按剩余毫秒设事务级statement_timeout；不是每条各给8秒。 |
| `backend/app/api/routes/dashboard.py` | 在首页仓储调用外启用上述预算；PostgreSQL 57014或预算耗尽转为可重试503 `dashboard_home_timeout`，由现有依赖回滚事务。 |
| `frontend/src/client.ts` | `apiRequest`支持可选timeoutMs，覆盖fetch和响应正文读取；超时／调用者取消后中止信号并清理计时器和监听器，保留Cookie、CSRF和迟到401身份保护。未给其他请求或上传统一缩短时限。 |
| `frontend/src/api.ts` | 仅 `getDashboardHome()` 使用12秒预算。 |
| `frontend/src/pages/HomePage.tsx`、`homeLayout.css` | 首页关闭自动重试，失败后暂停周期刷新并提供手工重试。首屏失败退出骨架；已有数据时保留历史快照，显示刷新错误和当前状态未知，撤下绿色实时标记。原大屏构图不变。 |
| `frontend/src/App.tsx` | 基线lint发现条件路由对象每次重建，使用useMemo稳定对象；角色与路由值不变。 |
| 新前后端测试及 `App.curveEntry.test.tsx` | 新增超时、取消、恢复、统计等价等回归；旧工厂路由测试仅修正已过时的可访问文案断言，未改工厂产品代码。 |

SQL事件监听器仅绑定本次借出的连接，在finally中移除；SET LOCAL随提交／回滚恢复。隔离测试验证同一pg_backend_pid重用后超时设置及监听器数量均恢复，没有留下超时工作线程或连接上的pending监听。数据库连接获取及认证发生在该仓储预算之前，8秒不是整个HTTP链路的绝对预算；浏览器12秒预算用于限制用户等待。浏览器取消等待本身不能被当作数据库已取消，数据库另有上述真实语句预算。

## 验证与生效状态

- 前端 `npm run check`：48个测试文件、389项通过，lint、TypeScript和生产构建通过。其中新增10项请求截止测试和2项首页恢复测试。
- 隔离PostgreSQL后端29项通过。新回归验证原／新SQL统计相同，研发included10／excluded20／pending30秒分层正确，FACTORY／UNCLASSIFIED／void记录隔离、无执行对象与用例保留、无计划权限账号不可读其他执行。
- 实际pg_sleep验证累计预算超时，真实Cookie HTTP验证503后下一请求恢复200，连接池重用与监听器清理通过。隔离数据库和文件已清理。
- 镜像导入检查通过；部署后API三份运行源码逐字节匹配修复工作区；实际网页主bundle含12秒超时逻辑，且不含待合并的“机器人总览”功能。

已仅更新当前站点API和网页。版本 `3.0.21-homefix1`，API启动时间16:51:43，健康检查通过；网页随后更新并健康。部署前后逐一核对，PostgreSQL、计算worker、归档worker、Pi的容器身份、镜像、启动时间与健康状态未改。本轮无数据库迁移、原始数据修改、上位机安装、PLC/CAN/机器人操作；整机工厂接入仍在原本地分支，未混入此次更新。

现场证据、镜像构建、部署和回滚配置保存在原项目忽略目录 `outputs/home-loading-20261105/`。关键文件为 `web-timeline.json`、`database-snapshot.txt`、`profile-home-*.log`、`profile-post-deploy.log`、`deployment-verification.json`、`before-containers.json`、`after-containers.json`、`before-platform.override.yml`、`candidate-platform.override.yml`。不提交这些运行资料和凭据。

浏览器管理策略此前拒绝访问，本轮未通过另一个浏览器、headless或其他地址绕过；未进行真实浏览器像素截图验收。上线确认依据是源码／bundle核对、服务健康、真实客户端首页200日志和现场只读查询，不把隔离界面测试当作现场截图。也未进行几千小时稳定性试验。

## 后续使用与合并

刷新网页可加载新版资源。超时后可以手工重试；后台失败保留的页面明确为历史快照。若再次持续超时，应按请求ID、SQL活动及剩余预算检查，不通过反复刷新制造更多并发查询。

后续整机功能合并时先包含本修复，复核App/HomePage/API相关冲突，再运行双方回归；不要用旧整机候选镜像覆盖已修复的首页代码。旧镜像和部署覆盖配置已保留，回退只涉及API和网页，不删除数据库或原始归档。
