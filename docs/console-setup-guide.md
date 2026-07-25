# 手动创建 AgentCore Gateway(Console 指南)

本指南用于**在已完成 `./deploy.sh` 部署之后**,在 AWS 控制台里**手动再新建一个
AgentCore Gateway 及其 4 个 Target**,以便亲手体验网关的搭建流程。

`deploy.sh` 已经创建好全部依赖资源——Cognito、3 个服务的 API Gateway
`execute-api` 端点、VPC Link、S3 中的 OpenAPI 规范、以及 Token Vault 里的出站凭证
提供者。本指南**直接复用**这些资源,只手动新建网关本身,因此把新网关命名为
`<project>-gateway-manual`,与自动创建的 `<project>-gateway` 区分开。

> 区域固定为 `ap-northeast-1`。文中 `<project>` 替换为你的 `PROJECT_NAME`
> (默认 `agentcore-gw-demo`)。所需的具体值见[附录:取值速查](#附录取值速查)。

> **要点:聚合型 MCP 网关的每个 Target 必须带工具 schema**,因此 4 个 Target 全部
> 使用 **OpenAPI → MCP** 或 **MCP Server** 类型。HTTP Passthrough **不适用于**
> MCP 协议网关(会被拒绝),故本方案不使用它。

---

## 步骤 1:创建 AgentCore Gateway

Amazon Bedrock 控制台 → **AgentCore** → **Gateways** → **Create gateway**。

| 字段 | 值 |
|------|-----|
| **Name** | `<project>-gateway-manual` |
| **Protocol** | **MCP** |
| **Supported versions** | `2025-06-18` |
| **Search type** | **Semantic**(可选,启用工具语义检索)|
| **Inbound auth** | **OAuth / JWT**(底层 `CUSTOM_JWT`)|
| **Discovery URL** | `https://cognito-idp.ap-northeast-1.amazonaws.com/<userPoolId>/.well-known/openid-configuration`(栈输出 `CognitoDiscoveryUrl`)|
| **Allowed clients** | **gateway-client ID**(栈输出 `GatewayClientId`)|
| **IAM execution role** | 复用 `deploy.sh` 创建的执行角色(栈输出 `GatewayRoleArn`)|

> 执行角色需具备:`s3:GetObject`(读 OpenAPI 规范)、
> `bedrock-agentcore:GetResourceOauth2Token` / `GetResourceApiKey` /
> `GetWorkloadAccessToken`(用 Token Vault 凭证)、以及对
> `secretsmanager:...:secret:bedrock-agentcore-identity!default/*` 的
> `secretsmanager:GetSecretValue`(读凭证提供者的托管密钥——**缺此权限会导致所有
> 工具调用报 "internal error"**)。`GatewayRoleArn` 角色已包含这些权限。

约 1-3 分钟后状态变为 **`READY`**,详情页显示 **MCP endpoint URL**(形如
`https://<gateway-id>.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp`),
记下备用。

![截图占位](images/step1-create-gateway.png)

---

## 步骤 2:复用出站凭证提供者(Token Vault)

`deploy.sh` 已在 AgentCore Identity 的 Token Vault 中创建两个提供者,可直接复用
(它们属于账号/Vault 级别,不绑定单个网关):

| 提供者 | 名称 | 用途 |
|--------|------|------|
| OAuth | `<project>-cognito-oauth` | Target 1/2/3 出站鉴权(Cognito service-client)|
| API Key | `<project>-jira-apikey` | Target 4 出站(Jira Basic 认证)|

在 **AgentCore → Identity → Credential providers** 中确认二者存在,记下各自的
**Credential Provider ARN**(Target 引用时用)。若不存在(例如未配置 Jira),可参考
`gateway/setup-gateway.py` 手动创建;OAuth 提供者用 service-client 的 ID/密钥 +
`CognitoDiscoveryUrl` + scope `<project>/invoke`。

---

## 步骤 3:添加 4 个 Target

在 `<project>-gateway-manual` → **Targets** → **Add target**,依次添加下列 4 个。
端点/规范取值见[附录](#附录取值速查)。全部出站鉴权:Target 1/2/3 选 **OAuth**
(引用 `<project>-cognito-oauth`,scope `<project>/invoke`);Target 4 选 **API Key**。

| # | Name | Target 类型 | 关键配置 |
|---|------|-------------|----------|
| 1 | `langgraph-agent` | **MCP → OpenAPI Schema** | S3 URI:`.../langgraph-agent/openapi.yaml`;出站 OAuth |
| 2 | `claude-agent` | **MCP → OpenAPI Schema** | S3 URI:`.../claude-agent/openapi.yaml`;出站 OAuth |
| 3 | `mcp-server` | **MCP → MCP Server** | Server URL:MCP 服务 `execute-api` 端点 + `/mcp`;出站 OAuth |
| 4 | `jira-cloud` | **MCP → OpenAPI Schema** | S3 URI:`.../jira-cloud/openapi.yaml`;出站 **API Key**(HEADER / `Authorization` / 前缀 `Basic `)|

要点:

- **OpenAPI Target(1/2/4)**:Gateway 把规范里每个操作转成一个 MCP 工具。规范中
  `servers` 的主机必须是可达的 `https://`——服务 1/2 是各自的 `execute-api` 主机名,
  Jira 是 `https://<你的域>.atlassian.net/rest/api/3`。`deploy.sh` 上传规范时已替换
  好占位符。
- **MCP Server Target(3)**:直接与该 MCP 服务握手并聚合其工具,URL 末尾要带 `/mcp`。
- 未配置 Jira 时可跳过 Target 4。

> **为什么 Jira 用 OpenAPI Target,而不是 console 里的 "Integrations" connector?**
> 添加 Target 时 console 会看到 **Integrations**(API 里是 `mcp.connector`),选 Jira
> provider 就能用 AWS 预置的 Jira 集成,似乎更省事。但**第三方 connector 需要先在
> AWS Marketplace 订阅对应的 partner tool** 才会在账号里变为可用——未订阅时用 API
> 直接建 connector target 会报 `Connector integration jira is not available for this
> account.`(实测 `jira` / `atlassian-jira` / `com.atlassian.jira` 等所有变体都如此;
> 而 AWS 原生的 `bedrock-knowledge-bases` 只报缺参数,可见这是账号级订阅门槛,不是
> ID 拼写问题)。此外 AWS 官方 Jira 集成走的是 **3-legged OAuth**(需 Jira 管理员安装
> Forge app 并授权一组 scope),与本 Demo 的 **API token / Basic 认证** 机制不同。因此
> 本方案对 Jira 统一采用可脚本化、用 API-key 出站的 **OpenAPI → MCP** Target。若你的
> 账号已在 Marketplace 订阅了 Jira connector,可自行改用 Integrations Target。

每个 Target 创建后进入同步,最终变为 **`READY`**。

![截图占位](images/step3-targets.png)

---

## 步骤 4:验证

Target 全部 `READY` 后,用 gateway-client 换取入站令牌并调用网关(注意必须带
`MCP-Protocol-Version: 2025-06-18` 头,否则报 400 版本不支持):

```bash
TOKEN=$(curl -s -X POST "<CognitoTokenUrl>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "<GatewayClientId>:<GatewayClientSecret>" \
  -d "grant_type=client_credentials&scope=<project>/invoke" | jq -r .access_token)

curl -s "<你的-manual-网关-MCP-endpoint>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

预期:返回聚合自 4 个 Target 的工具列表(工具名形如 `langgraph-agent___chat`、
`jira-cloud___searchIssues` 等)。

> 项目自带的 `gateway/validate.py` 也可做同样校验(默认针对自动创建的网关)。

---

## 步骤 5:在 Amazon Quick / Kiro 中连接

在客户端的 MCP 服务器配置中填写:

| 配置项 | 值 |
|--------|-----|
| **Server URL** | 你的 manual 网关 MCP endpoint |
| **Transport** | Streamable HTTP |
| **Authorization** | `Bearer <access_token>`(同上,gateway-client 签发)|

连接后列出工具并调用任意一个,确认返回正常。

---

## 附录:取值速查

`deploy.sh` 把值写入 `cdk-outputs.json`、`deploy-outputs.env`、
`gateway/generated-apigateway.json`。常用取值:

| 值 | 来源 |
|----|------|
| User Pool ID | `cdk-outputs.json` → `<project>-cognito`.`UserPoolId`(`deploy-outputs.env`:`COGNITO_USER_POOL_ID`)|
| Discovery URL | 栈输出 `CognitoDiscoveryUrl` |
| Token URL | 栈输出 `CognitoTokenUrl` |
| Scope | `CognitoScope` = `<project>/invoke` |
| gateway-client ID | 栈输出 `GatewayClientId` |
| service-client ID | 栈输出 `ServiceClientId` |
| Gateway 执行角色 ARN | 栈输出 `GatewayRoleArn` |
| 各服务 execute-api 端点 / API ID | `gateway/generated-apigateway.json` → `<service>.endpoint` / `.apiId` |
| OpenAPI 规范 S3 URI | `s3://<project>-gateway-specs-<accountId>/<service>/openapi.yaml` |

**读取示例:**

```bash
jq -r '."agentcore-gw-demo-cognito".GatewayRoleArn' cdk-outputs.json
jq -r '."mcp-server".endpoint' gateway/generated-apigateway.json
# 客户端密钥(勿落盘):
aws cognito-idp describe-user-pool-client --user-pool-id <userPoolId> \
  --client-id <GatewayClientId> --region ap-northeast-1 \
  --query 'UserPoolClient.ClientSecret' --output text
```

## 常见问题

- **工具调用报 "An internal error occurred"**:网关执行角色缺少对
  `bedrock-agentcore-identity!default/*` 的 `secretsmanager:GetSecretValue`(取不到
  出站凭证);或(OpenAPI Target)缺 `s3:GetObject`。开启网关日志投递
  (CloudWatch delivery-source,`APPLICATION_LOGS`)可看到真实错误。
- **`tools/list` 报 400 Unsupported protocol version**:请求需带头
  `MCP-Protocol-Version: 2025-06-18`。
- **入站 401**:令牌须由 **gateway-client** 签发且在网关 Allowed clients 列表中,
  并带 `<project>/invoke` scope。
- **`mcp-server` Target 调用报错 / 服务返回 `421 Misdirected Request`**:MCP SDK
  `mcp >= 1.23` 默认启用 **DNS rebinding 防护**(校验 Host 头,修复
  GHSA-9h52-p55h-vw2f)。FastMCP 的 `host` 默认是 `127.0.0.1`,此时会**自动开启**
  该防护且只允许 localhost——经 API Gateway / 内部 ALB 转发进来的请求会全部 421。
  本项目已把它显式化为环境变量:`MCP_ALLOWED_HOSTS`(逗号分隔的 Host 白名单,
  支持 `host:*` 任意端口写法;`deploy.sh` 在 API Gateway 创建后自动填入
  execute-api 与 ALB 两个主机名并重新 apply Deployment)、
  `MCP_DNS_REBINDING_PROTECTION`(设为 `false` 可整体关闭)。注意白名单**不支持
  域名通配符**,只支持精确主机与 `host:*`。`/health` 在 MCP mount 之外,不受影响。
- **Target `FAILED`**:检查执行角色权限、S3 规范可达、`servers` 主机为有效
  `https://` 端点;修复后点 **Sync**。
- **Jira `searchIssues` 报错**:Atlassian 已移除旧的 `/rest/api/3/search`,须用
  `/rest/api/3/search/jql`(本项目规范已更新),且 JQL 需有界(加项目或时间限制),
  例如 `project = ACGW ORDER BY created DESC`。
- **Jira `createIssue` 报 "internal error"**:网关把 Jira 的 HTTP 400 屏蔽成了通用
  错误。常见原因是项目/issue type 不合法——discovery 项目只支持 `Idea`。`deploy.sh`
  已幂等创建软件项目 `JIRA_PROJECT_KEY`(默认 `ACGW`,支持 `Task/Story/Bug`),
  调用时用该 project key + 合法 issue type 即可。
