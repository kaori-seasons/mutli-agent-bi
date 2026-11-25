# P1 可审核样例

- `registry-demo.json`：完整 v2 契约，source_id 为 a，数据库 db_a，物理表 orders(city, amount)。包含数据源、数据集、字段和按城市求和模板；只是合成示例，不能当作真实业务审批结果。
- `sample-migration-report.json`：使用既有抽取器读取 assets/orders_default.xlsx 后的迁移摘要；21 个实体完整保留，3 个指标缺少必要口径而阻断执行。报告不包含完整原快照，正式迁移文档会完整保存原文。

使用前按目标环境修改 source_id/database/owner/定义并独立审核。提交、摘要审核、发布和查询方式见 [P1 实施记录](../../implementation/2026-11-25-p1-registry-and-query-integration.md)。离线 validate 只校验结构与引用；实际源绑定和 SQL 策略会在注册、发布、执行阶段复核。
