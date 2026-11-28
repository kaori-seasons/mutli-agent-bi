# mutli-agent-bi

> P1 已落地：Registry v2、双人审核、不可变发布、CAS 回滚及已发布模板查询。默认保持 P0 路径；启用方式和验证证据见 [P1 实施记录](docs/implementation/2026-11-25-p1-registry-and-query-integration.md)。

> P2 已落地：MySQL 白名单元数据与聚合画像、复合键关系证据、自动候选编译和 `/ontology-review` 审核台。采集默认关闭，自动结果不进入发布区；人工移交后仍须独立审核与显式发布。配置、限制、测试和回退步骤见 [P2 实施记录](docs/implementation/2026-11-27-p2-candidate-discovery-and-review.md)。

> P3 工程链路已落地：已批准 MetricContract → 结构化计划 → 确定性指标 SQL → P0 网关 → 私有执行证据。支持受控单源聚合、明确多对一连接、强制政策、幂等执行和政策变更后的结果/下载失效；默认放量比例为 0。API、合成评测、业务验收边界和回退步骤见 [P3 实施记录](docs/implementation/2026-11-28-p3-controlled-planning-and-metrics.md)。

> P3 [本地生产工程验收](docs/implementation/2026-11-28-p3-local-production-acceptance.md)通过：255 项测试，覆盖签名 HTTP、真实 MySQL、10 客户端负载与恢复演练。灰度仍为 0；本地目录拒答对照不替代 E1 模型质量对照或真实业务放量验收。

> 2026-11-25：已接入 P0 身份认证、受控 SQL 执行与审核式多库聚合。升级配置、测试证据和生产边界见 [P0 实施记录](docs/implementation/2026-11-25-p0-security-and-federation.md)。首次升级必须配置 `AUTH_SOURCE_GRANTS`；默认不授予查询权限。

基于多Agent构建的chatbi
能够帮助企业快速构建自己的会话式BI工具，帮助分析数据而无需复杂的人工。一般情况构建生成式BI，要提高BI响应的准确性，需要对数据提供尽可能详尽的描述，帮助大模型理解数据的业务含义。
这称之为提示词工程。本方案基于多角色Agent协同技术，简化提示词工程的复杂性，构建高准确度的生成式BI
![图示](assets/demo1.png)

## 项目初衷
1. 解决业务逻辑及业务术语繁杂，提示词工程复杂度高带来的问题。
2. 解决提示词的微调稍有不慎造成影响扩散。
3. 解决涉及多表的复杂查询无法生成准确的SQL。

## 主要功能
1. 使用自然语言查询数据，并生成图表
2. 自动生成提示词，并使用多角色Agent协同技术对提示词进行优化，提高查询准确性
3. 支持自定义SQL模板，解决复杂SQL的生成问题
4. 使用 Cognito 可信身份和服务端授权实现数据源级权限；行列范围需由数据库审核视图/账号控制，完整行列策略尚未实现
5. 支持经过审核契约的多源分区聚合，按指标代数合并；失败或截断不返回完整总计
6. 支持代理部署模式，数据查询服务和模型推理服务分离部署

## 应用场景
本方案以会话的形式和数据交互，以表格和图表展示数据，并对数据进行充分的解释，帮助你理解数据，进行数据探索和分析，并提供运营建议


## 架构

项目使用多角色Agent 协同构建提示词模板，提高查询准确性。提示词模板是对被查询数据的描述，用于让大模型理解数据的含义，从而回应用户的查询
![提示词优化](assets/chatbi-prompt.drawio.png)
1. Prompt Engineer Agent 从源库获取涉及的表的元信息和采样数据，经过分析后生成提示词模板
2. Data Engineer Agent 使用上个步骤产生的提示词模板，和测试问题集合，生成SQL,并执行获取返回的结果
3. Test Engineer Agent 根据用户提供的测试问题集的信息，和上个步骤返回的数据，评判生成SQL的质量，生成反馈
4. Prompt Engineer Agent 根据反馈重新生成提示词模板，进入下一轮迭代优化
5. 最终生成的最优提示词模板被做为实际推理的提示词上下文


项目整体架构如下
![系统架构](assets/arch.png)
1. 用户发起请求后，用户问题进行embedding 
2. 系统根据用户问题查找向量库寻找合适的提示词模板
3. 系统根据提示词模板构建问题上下文
4. 系统提交问题到大语言模型，获得推理结果，生成SQL和问题建议
5. 系统使用SQL 向底层数据库查询数据，并对返回的数据进行加工，合并，处理
6. 将数据和建议返回客户端


## 部署
### 前提条件
1. 安装依赖
```
pip install --require-hashes -r requirements.lock
```
2. 项目使用了 Amazon Cognito 用户池进行身份认证登录，请创建Cognito 用户池，并添加访问用户

3. 配置
根据 `.env_template` 配置身份服务、数据源与显式授权。生产通过平台注入环境变量；本地启动如需读取 `.env`，显式设置 `APP_ENV_FILE=.env`。多源模板、源场景映射及只读账号要求见 P0 实施记录。

### 生成和优化提示词
1. 请使用如下命令查看相关命令用法
```
python prompt_gen.py
```
2. 使用如下命令生成提示词模板
```
python prompt_gen.py template --scenario {场景名称} --tables {表名1} {表名2} {表名3}
```
上述命令会对传入的表进行提示词自动生成，最终会生成一个 场景名称.xlsx 的提示词模板文件，位于
```
/prompt/data/promptdata/{场景名称.xlsx}
```
如果这个场景是使用者默认查询最多查询场景，给文件名添加_default后缀

例如：
```
python prompt_gen.py template --scenario ecommerce --tables order_detail users goods
```
生成的提示词模板位于
```
/prompt/data/promptdata/ecommerce.xlsx
```
如果这个场景是使用者默认查询最多查询场景，则修改ecommerce.xlsx 为 ecommerce_default.xlsx

3. 你也可以打开该提示词模板文件，根据你对业务的了解进一步的审核和修改. 
这里有一个使用demo数据生成的提示词模板，你可以下载下来参考
```
/assets/orders_default.xlsx
```

4. 如果你对提示词模板文件进行了内容上的调整和修改，请重新放会/prompt/data/promptdata/ 目录

5. 使用如下命令生成最终的提示词
```
python prompt_gen.py template
```
上述命令会生成最终的版本的提示词并且上传到配置文件中指定的s3 目录

### 运行ChatBI
1. 你可以使用提供的Dockerfile分别构建前后端部署在虚拟机或者k8s中，详细步骤不再赘述。这里以部署在虚拟机中举例
```
# build 前端
docker build -t text2sql_dev .

# 启动前端
docker run --env-file=.env -p 5017:8900  --name textdemo2 text2sql_dev

# build后端
docker build -f DockerfileServer -t text2sql_server .

# 启动后端
docker run --env-file /secure/runtime.env -p 127.0.0.1:5018:80 \
  -v /secure/query-exports:/app/var/exports \
  --name textdemoserver text2sql_server
```
使用浏览器打开ip:5017

## 补充信息

### 查询结果下载

P0 使用受身份和数据源权限保护的 `/api/downloads/<id>`，链接 10 分钟有效；通过 `QUERY_EXPORT_PATH` 配置私有存储。不要使用公开静态文件服务器暴露查询结果。单机/多节点限制与迁移方式见 [P0 实施记录](docs/implementation/2026-11-25-p0-security-and-federation.md)。

### 配置模型代理服务器
如何你的网络环境访问大模型不稳定，则需要使用代理
1. 修改.env配置文件，修改LLM_PROXY_SERVER参数，该参数是你可以访问大模型地区虚拟的IP及分配给代理服务的端口
2. 主服务与代理均配置相同的至少 32 字符随机 `LLM_PROXY_TOKEN`；通过受保护网络连接。运行如下命令启动本地代理服务
```
APP_ENV_FILE=.env python3 mainproxy.py
```
3.使用更新后的.env配置文件，重新启动chatbi的后的服务
```
APP_ENV_FILE=.env python3 main.py
```

