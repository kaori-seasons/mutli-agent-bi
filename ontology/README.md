# 本体知识引擎：第一阶段

> 本文保留 v1 文件与影子流程的兼容说明。新增 v2 注册、审核、发布、Knowledge API 和受控查询已独立实现，见 [P1 实施记录](../docs/implementation/2026-11-25-p1-registry-and-query-integration.md)。v1 的 approved 文件不会自动成为 v2 release；默认不切换查询。

本阶段是**设计、抽取、双写和旁路验证**。既有 Excel → Prompt JSON → S3 流程与 LLM → SQL 查询继续作为权威主链路；本体候选不进入 LLM Prompt，不改变查询返回值，不减少或增加 LLM 请求，不执行 SQL。尚未实现图库、向量库、HTTP KnowledgeAPI 或行列权限；不能把本体的审核过滤当作用户数据权限。

## 模型与模块

| 实体 | 主要语义与引用 |
| --- | --- |
| Scenario | name、description、query_rule、join_rule 原文、synonyms |
| Table | scenario_id、name、description、query_rule、synonyms |
| Column | table_id、name、dtype、description、is_dimension/is_measure、enumerable、option_desc、synonyms、primary_key |
| Relation | scenario_id、left_table/left_key、right_table/right_key（均为实体 id）、join_type |
| Metric | scenario_id、name、expression、column_ids、aggregation、filters、synonyms |
| QueryTemplate | question、params 示例值、content、结构化 slots、outputs、synonyms、question_pattern |

所有实体带 id、version、source（namespace/asset/location/producer）及 review_status（pending/approved/rejected）。id 根据实体类型、命名空间、场景/表/列或模板问题的业务身份确定性生成；改名是新身份，不会把不同数据源的同名实体合并。每个场景资产由一个工作簿负责，重复定义会拒绝发布。

- `schema.py`：dataclass 与严格 JSON 契约；校验未知字段、精确类型、重复字段、审核状态、实体重复及引用完整性。
- `store.py`：OntologyStore 抽象接口、FileOntologyStore，后续图库适配器实现同一接口。
- `extractor.py`：复用 PROMPT_F 已生成的 JSON 和物理 schema；读取既有 Excel 与 SQL 模板，不额外访问 LLM/数据库/S3，不保存采样行或连接配置。
- `service.py`：只读、确定性、稳定排序的关键词/同义词及 slot 骨架召回；无关问题返回空候选。
- `shadow.py`：进程内有界后台队列，只记录对比数据。
- `__main__.py`：离线抽取、校验、重试、来源移除、历史与回退。

## 审核与双写边界

1. `python prompt_gen.py template --scenario ... --tables ...` 保留既有 LLM 分批分析与 Excel/JSON 产出，然后将**全部表**的分析写入 `pending.json`。来源 producer 标记为 PROMPT_F，不自动批准模型猜测。
2. 人工编辑 Excel 后，`python prompt_gen.py prompt` 保留原确认提示、Prompt 生成与三次 S3 上传。三次上传均成功才读取 Excel，写入 `approved.json`；用户取消不写入。
3. 度量标记只产生 Metric 候选，aggregation 为空且保持 pending，即便 Excel 已确认审核，也不自动猜测 SUM、AVG、过滤条件等业务口径。
4. 不可解析的关联原文保留并附警告。明确的表列等式可以生成关系，但 join_type 为 unknown，关系保持 pending；明确给出 join_type 的结构化关联可随 Excel 审核批准。
5. 本体失败只记录错误类型及离线重试提示，保留既有产物，不回滚 S3/Excel，不阻断命令或线上查询。双写是**可重试的最终一致性**，不是跨 S3/本地文件分布式事务。

既有 S3 上传函数原来吞掉失败且没有状态，现在以 True/False 通知调用方；仍保持原上传参数、输出和异常吞掉行为。任一上传失败时所有原上传调用仍执行，但本体审核区不前进。

### Excel 与模板输入约定

Excel 第一张 sheet 的前四行保持既有布局：场景、场景描述、查询规则、关联规则，值位于第二列。表 sheet 前三行为表名、基本信息、查询规则；此后为字段数据，支持有/无字段标题行。字段列顺序沿用原有六列，可选第七列提供同义词（逗号分隔）。False/false/0/否表示禁用列；空字段名终止该表；NaN/空值不转成 `nan` 文本。

summary 可增加 `同义词` 行，也可增加 `结构化关联` 行，其第二列保存如下 JSON；原第四行关联原文仍完整保留：

```json
[{"left_table":"orders","left_key":"customer_id","right_table":"customers","right_key":"id","join_type":"left"}]
```

允许的关联类型是 inner/left/right/full/unknown。不解析模糊业务描述或未经验证的 SQL 片段，不猜测外键与关联类型；所有端点必须属于同一场景中的保留列。

SQL 模板支持现有 `summary.json` 的 question/params/sql 布局，以及 conf 的 `{question: {params, content}}` 字典。params 是示例参数而非业务主语；默认生成 `param_0` 等位置 slot，仅从值类型确定 dtype。SQL 占位符只接受 `{0}`、`{1}` 等显式位置形式，必须与 params 一一对应，拒绝属性访问和转换表达式。SQL 文件必须位于模板目录内。

可在 summary 项增加 slots/outputs/synonyms/question_pattern；例如：

```json
{
  "question":"上海订单数量",
  "params":["上海"],
  "sql":"count.sql",
  "slots":[{"name":"city","position":0,"dtype":"string","required":true,"synonyms":["城市"]}],
  "outputs":["订单数量"],
  "synonyms":["上海订单总数"],
  "question_pattern":"{0}订单数量"
}
```

slot 类型支持 string/integer/number/boolean/list。位置必须唯一、连续且与 params 一一对应。自动骨架只替换能在问题中明确定位的示例值；例如旧资产的“上海市”不等于问题里的“上海”，不会擅自推断地名变体，此时用词汇召回。明确填写 question_pattern 能提高召回质量，但不代表条件推理或参数绑定已上线。模板字符串 slot 的匹配长度上限为 96 字符（list 为 192）；最多 32 个 slot，问题/骨架不超过 4096 字符，采用有限状态匹配避免通配符指数回溯。

## 离线使用与恢复

使用 Python 3.9+（后端镜像为 3.12），以及项目已有 openpyxl；自动测试还使用已有 pandas，无新增依赖。全局参数 `--store`、`--namespace` 放在子命令之前。下列命令只访问指定本地资产，不读取 .env，不访问数据库/AWS：

```sh
# 从仓库真实样例生成待审核快照
python -m ontology --store ./var/ontology --namespace demo extract --workbook assets/orders_default.xlsx
python -m ontology --store ./var/ontology validate --stage pending

# 明确确认人工审核后的离线双写重试
python -m ontology --store ./var/ontology --namespace demo extract --workbooks prompt/data/promptdata --templates server/prompt_sqls --reviewed
python -m ontology --store ./var/ontology validate

# 独立校验任意完整快照（含修订哈希）
python -m ontology validate --file ./var/ontology/approved.json

# 模板可以独立同步；只替换同命名空间的 sql_templates 来源
python -m ontology --store ./var/ontology --namespace demo extract --templates server/prompt_sqls --reviewed

# 历史恢复：revision 使用 history 命令输出的完整值
python -m ontology --store ./var/ontology history --stage approved
python -m ontology --store ./var/ontology rollback <revision> --stage approved

# 明确移除来源，包括最后一个工作簿；历史仍可回退
python -m ontology --store ./var/ontology --namespace demo remove obsolete.xlsx --stage approved
```

分析 JSON 的离线重试格式为 `[{"analysis": PROMPT_F结果, "schema": get_table_schema结果}, ...]`，使用 `extract --analysis analysis.json --scenario 场景名`；只能进入待审核区，不能用 `--reviewed` 绕过 Excel 审核。

命令成功退出码 0，输出修订、数量和警告；失败退出码 1，仅输出错误类型。没有快照的 validate 不会伪装校验成功。生产数据路径不应写入 Git。目录模式是该命名空间 Excel 的权威清单：删除的工作簿会清除；空目录拒绝整批发布，防止误删，确需清空时用显式 remove。单文件模式只替换该来源。模板来源只有显式指定 `--templates` 才替换，避免意外清除其他发布者的资产。SQL 模板抽取本阶段只支持本地目录；既有 conf 的 S3 模板分支本身尚未落地。

## 存储保证与边界

默认路径为项目根目录 `var/ontology`；支持 ONTOLOGY_STORE_PATH 或构造函数覆盖。pending.json 与 approved.json 完全隔离，线上不读取 pending 文件；审核区内的 pending/rejected 实体也不参加默认召回，其未批准的祖先不能被批准子节点绕过。

- POSIX flock 串行化写入，默认锁等待 10 秒；写入持锁后重读最新快照，来源级合并不会覆盖其他并发发布者。
- 临时文件 → 文件 fsync → 原子 replace → 目录 fsync。读者看到完整旧版或新版；格式与 SHA-256 修订验证失败则拒绝文件。
- 相同内容幂等；只有改变的实体 version 递增。版本高水位保留于 version_counters，删除再创建或回退也不复用已使用版本。
- 每个审核区默认保留最近 20 次发布。history 先保存完整版本再更新当前文件；回退保留内容、递增发生变化的实体版本。显式 rollback 也能恢复损坏的当前快照；不可直接手改已封存文件。
- 仓库的修订哈希用于完整性检测，不是签名或访问控制。目录权限默认 0700、文件 0600，应限制发布者权限。
- 本地快照上限 32 MiB；工作簿展开上限 64 MiB、每张 sheet 至多 20000 行、至多 256 张 sheet。大规模知识资产需要下一阶段存储适配器，而非放大限额。

这是单机/POSIX 文件系统方案，不宣称适用于不保证 flock、原子 rename 与 fsync 的网络盘。查询端可以只读共享完整目录，勿只挂载 approved.json 单文件，否则发布者原子替换后容器可能仍看到旧 inode。多机器应由受控发布流程分发完整快照/修订，下一阶段升级独立存储服务。

## 旁路启用与运维

默认 `ONTOLOGY_SHADOW_ENABLED=false`，没有后台线程和本体磁盘读取。`server.api.init()` 在环境加载后配置旁路；uWSGI fork 后不复用父进程队列/锁，子进程首次投递时只创建自己的工作线程。每个进程最多一个当前后台记录线程；线程是 daemon，退出最多等待 0.2 秒，未完成样本可丢弃。

| 配置 | 默认 | 合法范围 |
| --- | --- | --- |
| ONTOLOGY_STORE_PATH | 项目根/var/ontology（镜像 /app/var/ontology） | 本地目录 |
| ONTOLOGY_NAMESPACE | default | 非空，与生成端一致 |
| ONTOLOGY_SHADOW_ENABLED | false | true/false |
| ONTOLOGY_SHADOW_SAMPLE_RATE | 0.1 | 0..1 的有限数值 |
| ONTOLOGY_SHADOW_QUEUE_SIZE | 128 | 1..10000 |
| ONTOLOGY_TOP_K | 5 | 1..100 |
| ONTOLOGY_REFRESH_SECONDS | 5 | 0.1..3600 秒 |

非法旁路配置会报告错误类型并关闭旁路，不能阻止服务启动。请求线程只处理至多 4096 字的问题、采样与 put_nowait；磁盘、完整性校验、建索引、召回和日志均在后台。队列满丢弃样本，错误/丢弃计数进入后续成功日志。后台任务可能消耗 CPU 和内存，不能宣称绝对零开销；上线从低采样率开始，观察查询 p95/p99、进程 CPU 与丢弃率后再提高采样。

缓存只在新修订校验与索引全部成功后替换；损坏/不可读文件保留上次良好结果并记录 cache_error_type（应告警），首次无可用快照返回空候选。无快照为空修订快照，不代表召回已配置完成。

后端镜像现已包含 ontology 包。生成端用可写目录；查询端将同一目录只读挂载到 `/app/var/ontology`。先完成离线抽取和 validate，再以 ONTOLOGY_NAMESPACE 相同、ONTOLOGY_SHADOW_ENABLED=true 启动后端，JSON 对比日志输出到标准错误供容器日志系统采集。设置 false 并重启进程即可关闭，不需要删除本体或改 LLM 配置。

上述旁路验收完成后的 P0 加固已修复入口、接入 Cognito 可信身份和 SQL 执行防线、去除后端镜像的 `.env` 复制，并加入受控多库聚合及私有下载。当前状态、真实 MySQL 测试证据与尚待完成的生产验收见 [P0 实施记录](../docs/implementation/2026-11-25-p0-security-and-federation.md)。本体旁路自身的测试仍不代表整站生产验收。

## 对比日志与离线评估

事件 `ontology_shadow` 为单行 JSON，包含 timestamp、trace_id、branch、namespace、question_hash、快照 revision、候选 id/version/score/reason、legacy_selection_id、raw_selection_hash、top1_agreement、topk_agreement、relation_ids、recall_ms、legacy_match_ms、状态和累计丢弃/错误数。分支失败、无匹配及默认场景回退同样记录，不只统计成功样本。执行失败使用 ontology_shadow_error，仅记录异常类型，不打印异常原文。

新日志不含原始问题、模板参数值、SQL 或用户身份；原始 LLM 选择也仅记录哈希，trace_id 非安全格式时哈希化。question_hash 使用受限长度问题的 SHA-256，便于在受控离线标注集上关联；哈希不是匿名化保证，不应公开日志。问题在后台队列内短暂存在，容量有界。旧系统日志策略未被修改。

- 按 trace_id + branch 对比候选与既有选择，再按 revision 分组观察漂移。
- top-1/top-k 是与既有 LLM 的**一致性**，不是准确率。必须用人工标注的期望场景/模板计算 recall@k 和准确率，不能把 LLM 当真值。
- legacy_match_ms 是既有匹配阶段耗时，recall_ms 包括本体缓存刷新/召回，首次与热缓存应分开统计。
- 本阶段 LLM 调用完全未减少；不报告虚构的 token/成本节省。第二阶段才可用 A/B 测量真实节省。
- 样本队列饱和、停机、日志故障会丢样本，需结合 dropped_total/errors_total/cache_error_type 评估采样偏差。

只读 Python API 可以直接注入 `OntologyService(FileOntologyStore(path), namespace="demo")`，调用 find_scenario_candidates(question)、find_template_candidates(question)、get_relations(scenario_name_or_id)。模块级同名函数默认读取审核区的所有命名空间，跨源场景应显式注入 namespace；本阶段这只是数据源分区，不是授权接口。

## 测试与后续路线

```sh
python -m unittest discover -s tests -v
python -m compileall -q ontology tests prompt_gen.py server/api.py server/aws.py
```

测试使用真实 orders_default.xlsx 与构造工作簿，覆盖六类实体、严格校验、并发更新/原子失败/版本/回退、批次与多源抽取、审核过滤、slot/同义词召回、缓存故障、队列饱和/日志隐私，以及旁路关/开/异常时的 API 返回、LLM Prompt 和调用次数一致性；AWS/MySQL 都使用替身，不读取真实 .env。

后续阶段：先用人工标注证明召回价值，再将只读 KnowledgeAPI 候选提供给 LLM 裁决；以 OntologyStore 适配图库和统一向量服务；随后才切换路由、填充 slot 与动态上下文，并实施真正的行列权限；最后将 SQL 质量反馈接入指标口径审核、版本治理与冲突处理。
