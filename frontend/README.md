# RP1 前端实施说明

## 设计真源

正式前端必须同时遵守：

- `rp1-reliability-platform/-design/01-PRD.md`
- `rp1-reliability-platform/-design/05-ui-spec.md`
- `rp1-reliability-platform/-design/07-current-brand-visual-brief.md`
- `rp1-reliability-platform/-design/visual-render-v4-showcase`

`visual-render-v4-showcase` 是当前已确认的视觉实现基线。不应再使用通用 SaaS 卡片页或单纯接口调试页替代这套信息架构。

## 页面路由

- `?view=home&kind=whole`：测试总控首页
- `?view=durations&kind=whole`：测试时长台账
- `?view=mtbf&kind=whole`：MTBF 结论中心
- `?view=assets&kind=module`：模块样品中心
- `?view=asset&kind=whole&id=RP1-001`：样品详情
- `?view=test-case&kind=whole&id=REL-UPPER-001`：Test Case 详情
- `?view=execution&kind=whole&id=<public-id-or-code>`：Execution 详情

路由使用浏览器 History API，支持深链、前进和返回。主题偏好保存在本地并默认跟随系统。

## 数据边界

生产入口只挂载 `src/main.tsx`，不包含 fixture 适配器。所有业务数字来自 `/api/v1` read-model：

- 前端不计算 MTBF，只显示后端观察、统计和正式结论分区。
- `runtime_interval` 只表示闭合账本；开放 Execution 的 elapsed 单独显示。
- 空集合、缺失字段和未配置目标保持空值语义，不补造指标。
- 整机与模块在 URL、查询键、API 参数和页面标题中显式隔离。

## 构建

```bash
npm ci --no-audit --no-fund
npm run test
npm run typecheck
npm run build
```

Docker 入口为 `http://127.0.0.1:8088/`。
