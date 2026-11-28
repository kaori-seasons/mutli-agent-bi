# P3 本地生产工程验收记录

日期：2026-11-28（Asia/Shanghai）。范围按用户指定为本地验收；对应 [P3 实施记录](2026-11-28-p3-controlled-planning-and-metrics.md)和[升级方案第 12 节](../research/2026-11-25-auto-ontology-production-upgrade-assessment.md#section-12)。

## 1. 验收结论与交付状态

**本地生产工程验收通过；线上灰度保持 0。** 本次完成 P0/P1/P2 兼容回归、P3 受控指标编译、安全负向验证，以及真实 HTTP / MySQL 的并发、故障和恢复演练。测试总计 **255 项通过：Python 219 项（0 跳过、0 失败）+ 前端 36 项**。重复负载请求不重复计为测试用例。

这份结论支持当前代码在默认关闭 P3 的状态下提交、交付和继续试点准备，不等于已完成真实业务开放验收。没有访问业务数据库、实际 Cognito 或模型服务；没有加载应用 `.env` 执行验收；没有发布生产环境。

保留如下配置：

```dotenv
ONTOLOGY_PLANNER_SOURCES=[]
ONTOLOGY_PLANNER_ROLLOUT_PERCENT=0
```

本地测试中的 `5`、`100` 只存在于隔离测试进程，退出即还原；不会写入运行环境或 `.env_template`。自动候选仍只进入候选区，发布与 MetricContract 执行授权仍走人工审核。

## 2. 环境与证据

| 项目 | 实际环境 / 证据 |
|---|---|
| 后端测试 | Python 3.12.13，锁定依赖的隔离虚拟环境 |
| 数据库 | Docker 中的一次性 MySQL 8.0.46，仅绑定本机环回端口 |
| 身份认证 | 内存生成 RSA 2048 位密钥；真实 RS256 JWT、真实 HTTP、生产 `authenticate()` 校验 |
| JWKS 边界 | 仅将密钥获取器换成本地内存解析器；没有实际 Cognito 网络、密钥轮换或可用性验收 |
| 服务入口 | 真实 Flask 应用，经线程化 Werkzeug 环回 HTTP 服务；测试不伪造 principal |
| Registry / 导出 | 每个用例独立临时目录；不复用应用历史数据 |
| 源权限 | 随机前缀测试库，SELECT-only 执行用户；仅夹具管理员负责建库、故障注入和清理 |
| 前端 | Node 20.20.2，Vitest 36 项、TypeScript 检查和隔离 Next 生产构建通过 |
| Python 依赖 | `uv pip check`：34 个安装包兼容 |
| 后端镜像 | 原 `DockerfileServer` + `requirements.lock --require-hashes` 构建通过，Linux arm64；容器内 `pip check` 通过 |
| 机器记录 | [本地验收 JSON](../evaluation/2026-11-28-p3-local-acceptance.json)，包含实际指标、失败计数和关键源文件 SHA-256 |
| 语义结果记录 | [P3 合成评测 JSON](../evaluation/2026-11-28-p3-synthetic-results.json)，保留原始 E0–E3 的运行范围 |

实现测试入口为 `tests/test_planner_p3.py`、`tests/test_p3_mysql.py`、`tests/web/p3.test.ts`；新增验收入口为 `tests/test_p3_local_acceptance.py`，报告生成器为 `scripts/accept_p3_local.py`。报告要求全量 Python 成功且没有跳过才将 `local_gate` 写为 `passed`；不根据该字段打开线上开关。

生产镜像启动另行验证：Nginx → uWSGI → Flask，`/healthz` 返回 200；无业务配置的 `/readyz` 返回 503；不带身份的 `POST /v1/query-plans/validate` 返回 401，均带 `Cache-Control: no-store`。容器内 `pip check` 无依赖损坏。镜像标识为 `sha256:9ff1c1beb5ee74d5da667315aa762230bfdc83122206df646585bf9d995f0e3d`，仅为本地验收镜像，未推送镜像仓库。构建上下文仅含 `server/`、`ontology/`、`main.py`、`requirements.lock`、`DockerfileServer`，不含 `.env` 或运行数据库。该启动检查未将签名负载套件再次跑在 uWSGI 上，不能据此声称多进程容量也达到同一延迟指标。

## 3. 实际验收矩阵

| 门槛 | 实际验证 | 结论 |
|---|---|---|
| 旧功能兼容 | P0 SQL / 多库聚合、P1 注册审批与模板、P2 候选及审核回归；新路径仅显式调用 | 通过 |
| 批准语义 | 核验 SUM / AVG / ratio / COUNT DISTINCT、原始粒度、复合键、NULL、零分母、孤儿维度、半开时间窗和精确 Decimal 边界 | 合成黄金结果通过 |
| 候选隔离 | 无批准契约不能生成可执行计划；P2 自动结果先候选、再独立审核与契约 | 通过 |
| HTTP 身份 | 无 token、格式伪造、过期、错误 client、ID token 均 401；普通读者管理请求 403；跨主体计划 404 | 通过 |
| SQL 与授权 | 参数绑定、强制主体政策、源撤权、物理别名政策、元数据漂移、目录唯一约束、下载 / 缓存重验 | 负向回归通过 |
| E1 本地负对照 | 同源同结构化意图，仅保留目录时 404 且业务 SQL 调用为 0；恢复批准契约后 COUNT=5 | 安全与语义边界通过；非模型答案质量评测 |
| 并发执行 | 10 客户端、每人 10 次独立逻辑操作，保持相同幂等键重试限流；实际执行 MySQL | 100/100 结果正确，无错误主体数据 |
| 知识接口 | 热进程、10 客户端、200 次搜索 | P95 75.151 ms，达到该本地负载下 ≤300 ms 的目标 |
| 数据源故障 | 实际锁定 MySQL 只读账号；执行失败无结果；解锁后旧键仍回放失败，新操作恢复成功 | 通过 |
| 一致性恢复 | SQLite `Connection.backup`，应用切至恢复副本；release 和执行 ID 保留；重放不再次执行 SQL | 通过 |
| 灰度关闭与恢复 | 100→0 拒绝新规划、新执行及已有成功结果读取；health/readiness 仍正常；恢复 100 后原收据可读 | 通过 |
| 5% 分桶 | 固定 namespace 的 bucket=2 主体可用、bucket=16 被拒；重复稳定，撤销源 grants 后仍拒绝 | 通过，未修改默认值 |

并发执行的详细记录：

| 指标 | 实测 |
|---|---:|
| 逻辑查询总数 / 成功 / 错误结果 | 100 / 100 / 0 |
| HTTP 200 / HTTP 429 | 100 / 385 |
| 完成 100 次操作的墙钟时间 | 5.452 s |
| 端到端 P50 / P95 / 最大值 | 386.277 / 974.718 / 1559.783 ms |
| 重试策略 | 同幂等键，每 25 ms 退避，单操作最多等待 15 s |

所有 429 都如实保留在统计中。100% 指客户端有界重试后的逻辑完成率，**不是首次 HTTP 请求的成功率**。P95 包含签名、HTTP、限流等待、业务执行和结果处理，不包含预先创建计划的耗时，也不包含模型推理。现有每主体 1 / 全局 4 个执行上限继续生效，10 客户端会主动触发限流。实际业务客户端需要有界退避和稳定幂等键；不能无界高速重试。

夹具事实表仅 8 行，覆盖语义反例；上述耗时用于本机工程回归，不代表生产容量、冷启动、长时间稳定性或大表扫描成本。200 次知识请求是重复负载，不能写成 200 道业务黄金题。

## 4. E1 与生产放量的结论边界

方案中的 E1 是“同一新查询循环 + 仅物理目录”，用于隔离本体带来的真实答案质量增益。本次新增的是 **E1 本地目录拒答负对照**：验证受控规划器缺少批准指标时不猜测 SQL。它不能回答“相比不使用本体提升多少百分点”，也不改变原合成报告中真实 E1 模型质量对照 `not_run` 的状态。

以下仍属于上线业务放量前的验收输入与动作，不能由本地合成数据补写为通过：

1. 业务 owner 审阅的至少 200 道分层题集、源快照、预期结果与可回答 / 应拒答标注。
2. 固定模型版本、温度、预算和权限的 E0/E1/E2/E3 配对评测；至少 3 次随机重复、置信区间、失败切片和成本统计。
3. 实际 Cognito / JWKS、代理 / TLS、目标数据源和只读权限的环境联调；实际业务表规模的冷热与持续负载。
4. 真实口径、召回、拒答和执行正确率达到方案门槛，确认上线负责人及回退窗口后再单独批准非零灰度。

因此最终记录同时保留 `local_gate=passed`、`online_rollout_authorized=false`、`online_rollout_percent=0`。没有用“本地通过”覆盖未验收的业务条件。

## 5. 可复现步骤

在不加载应用 `.env` 的终端中，使用按 `requirements.lock` 安装的 Python 环境和现有前端锁文件。以下数据库密码仅为一次性公开夹具值，不是生产凭据；只允许指向本机新建容器，脚本会创建和删除随机测试数据库 / 用户。

```bash
docker run -d --name bi-p3-local-test --publish 127.0.0.1::3306 \
  --env MYSQL_ROOT_PASSWORD=disposable-fixture-only mysql:8.0
docker port bi-p3-local-test 3306
# 等待 MySQL 就绪，将 <port> 换成上一步端口。
P0_MYSQL_TEST_PORT=<port> P0_MYSQL_TEST_ALLOW_CREATE=1 \
  P0_MYSQL_TEST_PASSWORD=disposable-fixture-only \
  .venv/bin/python scripts/accept_p3_local.py \
  --output /tmp/p3-local-acceptance.json
npx vitest run tests/web
npx tsc --noEmit --incremental false
# 仅在排除 .env*、var、.git 的临时副本中构建：
NEXT_TELEMETRY_DISABLED=1 npm run build
docker rm -f bi-p3-local-test
```

全量 Python 本次耗时 24.720 s；其中真实 MySQL / HTTP 用例需要显式 opt-in 和环回网络权限，未配置时默认跳过 31 项。验收脚本发现跳过则返回失败；普通开发环境的默认 discover 仍可按原方式运行。

## 6. 运维与回退实施要求

关闭 P3 应优先设置 `ONTOLOGY_PLANNER_ROLLOUT_PERCENT=0` 并按部署方式重新加载配置。它阻止后续受控规划、执行和结果读取；不等于立即取消已经发给数据库的语句。P0/P1 原入口保留，进行中的查询继续受预算和后置校验约束。

本次恢复演练验证的是一个一致性快照内的 release、计划和幂等执行记录，没有测量生产 RTO，也不承诺两个备份时点之间的 RPO=0。实际操作应先限制写入，使用 SQLite backup API 获取一致性副本并校验，不能在 WAL 活跃时仅复制主文件。恢复演练不包含源业务数据和外部导出文件的灾备。

P3 计划与结果 TTL 为 10 分钟，但 TTL 不会物理删除记录；累计 128 MiB payload / 10,000 条产物到限后拒绝新增，需要监控、备份与受控归档。不要删除幂等证据后复用旧键。旧 P1/P2 二进制不认识 MetricContract，二进制降级须恢复可读版本的一致性备份，不能仅回退代码后继续读取新发布历史。

本次实现、测试和验收材料合并为一个提交；原有 `need.md` 保留不纳入提交。
