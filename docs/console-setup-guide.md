# AgentCore Gateway 手动配置指南

本指南介绍如何在 **AWS 管理控制台 (AWS Management Console)** 中手动创建一个 Amazon Bedrock AgentCore Gateway 及其 4 个 Target，用来聚合 4 个企业级 AI 服务。

> 自动化方式：本项目提供 `deploy.sh` + `gateway/setup-gateway.py` 一键完成同样的配置。本指南是给希望**逐步理解每一步、或在生产环境按需手动操作**的读者准备的等价手动流程。

**默认区域 (Region)：`ap-northeast-1`（东京 / Tokyo）。** 本项目使用 **Amazon Cognito** 作为 OAuth 2.0 身份提供方 (IdP)。

本指南假设读者的项目前缀 `PROJECT_NAME` 为 `agentcore-gw-demo`（来自 `config.env`）。文中凡出现 `<project>` 之处，请替换为你自己的 `PROJECT_NAME`。

---

## 目录

- [前提条件](#前提条件)
- [步骤 1: 创建 AgentCore Gateway](#步骤-1-创建-agentcore-gateway)
- [步骤 2: 配置凭证提供者 (Token Vault)](#步骤-2-配置凭证提供者-token-vault)
- [步骤 3: 添加 Target 1 — LangGraph Agent](#步骤-3-添加-target-1--langgraph-agent)
- [步骤 4: 添加 Target 2 — Claude Agent](#步骤-4-添加-target-2--claude-agent)
- [步骤 5: 添加 Target 3 — MCP Server](#步骤-5-添加-target-3--mcp-server)
- [步骤 6: 添加 Target 4 — Jira Cloud](#步骤-6-添加-target-4--jira-cloud)
- [步骤 7: 同步 Targets 并验证](#步骤-7-同步-targets-并验证)
- [步骤 8: 在 Amazon Quick / Kiro 中连接 Gateway](#步骤-8-在-amazon-quick--kiro-中连接-gateway)
- [附录 A: 获取所需值的速查表](#附录-a-获取所需值的速查表)
- [附录 B: 常见问题排查](#附录-b-常见问题排查)

---

## 前提条件

在开始之前，请确认以下条件全部满足。

**1. 4 个示例服务已部署（或你自有的等价服务）**

本项目聚合以下 4 个服务。你需要已经部署了它们（通过本项目的 CDK + EKS 流程），或准备好自己的等价 HTTPS 端点：

| 服务 | 类型 | 说明 |
|------|------|------|
| Service 1 — LangGraph Agent | 自托管 HTTP 服务 | 通过 ALB 暴露 |
| Service 2 — Claude Agent | 自托管 HTTP 服务（有 OpenAPI 规范）| 通过 ALB 暴露 |
| Service 3 — MCP Server | 自托管原生 MCP 服务 | 通过 ALB 暴露，路径 `/mcp` |
| Service 4 — Jira Cloud | 外部 SaaS | Atlassian Cloud，无需部署 |

**2. 具备访问权限的 AWS 控制台账户**

- 能访问 `ap-northeast-1` 区域的 Amazon Bedrock → AgentCore 控制台。
- 具备创建 IAM 角色、读写 S3、访问 Secrets Manager、以及 `bedrock-agentcore-control` API 的权限。

**3. 一个已配置好的 Amazon Cognito 用户池 (User Pool)**

用户池必须包含：

- 一个 **资源服务器 (Resource Server)**，其标识符 (identifier) 为 `<project>`，且暴露一个自定义作用域 (scope) `invoke`，因此完整作用域字符串为 **`<project>/invoke`**（例如 `agentcore-gw-demo/invoke`）。
- 一个**托管域 (Hosted Domain)**，格式为 `https://<domain>.auth.ap-northeast-1.amazoncognito.com`，它提供 `/oauth2/token` 令牌端点。
- **两个应用客户端 (App Clients)**，均启用 **client credentials** 授权类型（机器对机器 M2M），均生成客户端密钥 (client secret)：
  - **service-client**（`<project>-service-client`）：Gateway 的**出站 (outbound)** 凭证提供者用它去获取访问服务 1/2/3 的令牌。
  - **gateway-client**（`<project>-gateway-client`）：代表 MCP 客户端（Amazon Quick / Kiro）向 Gateway 发起**入站 (inbound)** 调用；Gateway 的 JWT 授权器允许此客户端 ID。

> 本项目的 `<project>-cognito` CDK 栈会自动创建以上全部资源，并以栈输出 (Stack Outputs) 的形式给出所需值。见[附录 A](#附录-a-获取所需值的速查表)。

**4. Claude Agent 的 `openapi.yaml` 已上传到 S3**

Target 2 使用 OpenAPI 规范来发现工具，规范文件需存放在 S3，例如：
`s3://<project>-gateway-specs-<accountId>/claude-agent/openapi.yaml`。

> 上传前需将规范中的 `${CLAUDE_AGENT_HOST}`（仅主机名，不含 scheme）与 `${COGNITO_DOMAIN}` 占位符替换为真实值。`deploy.sh` 会自动完成这步替换与上传。

**5. ALB HTTPS 端点已就绪**

> **重要：AgentCore Gateway 的所有 Target 端点必须是 `https://`。** ALB 必须挂载 ACM 证书并配置 HTTPS(443) 监听器。仅 HTTP 的 ALB 无法用于创建 Gateway Target。请提前准备 `ACM_CERTIFICATE_ARN` 与解析到 ALB 的 `BASE_DOMAIN`。

![截图占位: prerequisites-overview.png](images/prerequisites-overview.png)

---

## 步骤 1: 创建 AgentCore Gateway

**(a) 导航路径 (Navigation)**

1. 登录 AWS 管理控制台，右上角切换区域至 **Asia Pacific (Tokyo) ap-northeast-1**。
2. 打开 **Amazon Bedrock** 控制台。
3. 在左侧导航栏选择 **AgentCore**（有些界面显示为 *Bedrock AgentCore*）。
4. 选择 **Gateways** → 点击 **Create gateway**（创建网关）。

**(b) 字段填写 (Fields)**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `<project>-gateway`（例如 `agentcore-gw-demo-gateway`，来自 `config.env` 的 `GATEWAY_NAME`）|
| **Description** | `Demo gateway aggregating 4 enterprise AI services`（可选）|
| **Protocol** | 选择 **MCP** |
| **MCP version / Supported versions** | `2025-06-18`（来自 `GATEWAY_MCP_VERSION`）|
| **Search type / Semantic search** | 启用 **Semantic**（语义搜索），对应 `GATEWAY_SEARCH_TYPE=SEMANTIC`。启用后可对聚合的工具做语义检索。|
| **Inbound authentication** | 选择 **OAuth / JWT**（在底层 API 中对应 `authorizerType = CUSTOM_JWT`）|
| **Discovery URL** | `https://cognito-idp.ap-northeast-1.amazonaws.com/<userPoolId>/.well-known/openid-configuration`。将 `<userPoolId>` 替换为你的 Cognito 用户池 ID（来自 `<project>-cognito` 栈输出 `CognitoDiscoveryUrl`）。|
| **Allowed clients / Allowed audience** | 填入 **gateway-client 的客户端 ID**（栈输出 `GatewayClientId`）。这确保只有 MCP 客户端持有的、由 gateway-client 签发的令牌才能调用网关。|
| **IAM execution role** | 见下方说明。|

**IAM 执行角色 (Execution Role) 要求**

Gateway 需要一个可被 `bedrock-agentcore.amazonaws.com` 服务主体担任 (assume) 的 IAM 执行角色。该角色至少需要以下权限：

- `s3:GetObject` —— 读取 S3 中的 OpenAPI 规范（资源限定到 `arn:aws:s3:::<project>-gateway-specs-*/*`）。
- `bedrock-agentcore:GetResourceOauth2Token`、`bedrock-agentcore:GetResourceApiKey`、`bedrock-agentcore:GetWorkloadAccessToken` —— 使用 Token Vault 中的凭证提供者。
- `secretsmanager:GetSecretValue` —— 读取 Jira API token 等密钥（限定到 `arn:aws:secretsmanager:<region>:<account>:secret:<project>/*`）。

> 本项目的 `<project>-cognito` CDK 栈已创建该角色，其 ARN 由栈输出 `GatewayRoleArn` 给出。在控制台中选择该已有角色即可；若手动创建，请按上述权限配置信任关系与策略。

**(c) 预期结果 (Expected Result)**

点击 **Create** 后，Gateway 进入 `CREATING` 状态，约 1-3 分钟后变为 **`READY`**。就绪后详情页会显示一个 **MCP endpoint URL**（网关的 MCP 端点，形如 `https://<gateway-id>.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp`）。请记下该 URL，步骤 8 会用到。

![截图占位: step1-create-gateway.png](images/step1-create-gateway.png)
![截图占位: step1-gateway-ready.png](images/step1-gateway-ready.png)

---

## 步骤 2: 配置凭证提供者 (Token Vault)

出站 (outbound) 凭证提供者存储在 **AgentCore Identity 的 Token Vault** 中，供各 Target 在调用后端服务时获取凭证。本项目需要两个：一个 OAuth 提供者（服务 1/2/3）与一个 API Key 提供者（Jira）。

**导航路径**：Amazon Bedrock → **AgentCore** → **Identity**（或 **Credential providers** / **Token Vault**）。

### 2.1 OAuth Credential Provider

用于 Target 1/2/3 向自托管服务做出站鉴权。

**(a) 导航路径**

AgentCore → Identity → **Credential providers** → **Create** → 选择类型 **OAuth (OAuth2)**。

**(b) 字段填写**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `<project>-cognito-oauth`（例如 `agentcore-gw-demo-cognito-oauth`）|
| **Credential provider vendor** | **CustomOauth2** |
| **Discovery URL** | `https://cognito-idp.ap-northeast-1.amazonaws.com/<userPoolId>/.well-known/openid-configuration`（栈输出 `CognitoDiscoveryUrl`）|
| **Client ID** | **service-client 的客户端 ID**（栈输出 `ServiceClientId`）|
| **Client secret** | service-client 的客户端密钥。通过 `aws cognito-idp describe-user-pool-client` 获取（见[附录 A](#附录-a-获取所需值的速查表)）。**切勿写入任何文件**，仅在控制台表单中直接粘贴。|
| **Scope(s)** | `<project>/invoke`（例如 `agentcore-gw-demo/invoke`，栈输出 `CognitoScope`）|

**(c) 预期结果**

创建成功后列表中出现该提供者，并生成一个 **Credential Provider ARN**。记下此 ARN，Target 1/2/3 的出站鉴权会引用它。

![截图占位: step2-1-oauth-provider.png](images/step2-1-oauth-provider.png)

### 2.2 API Key Credential Provider（用于 Jira）

Jira Cloud 使用 **Basic 认证**：`base64(email:api_token)`。

**(a) 导航路径**

AgentCore → Identity → **Credential providers** → **Create** → 选择类型 **API Key**。

**(b) 准备 API Key 值（先经由 Secrets Manager）**

> **重要：绝不要把 Jira API token 或 Cognito 客户端密钥写入配置文件或代码库。** 将 Jira API token 存入 AWS Secrets Manager（本项目默认密钥名 `<project>/jira-api-token`，即 `JIRA_API_TOKEN_SECRET_NAME`）。

API Key 提供者需要的值是 Basic 凭证串，其构造方式为：

```
base64( "<你的-atlassian-邮箱>:<你的-jira-api-token>" )
```

请从 Secrets Manager 读取 token 后在本地/可信环境计算该 base64 串（不要将 token 落盘到项目文件）。

**(c) 字段填写**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `<project>-jira-apikey`（例如 `agentcore-gw-demo-jira-apikey`）|
| **API key** | 上面计算得到的 `base64(email:api_token)` 值 |
| **Credential location** | **HEADER** |
| **Parameter name** | `Authorization` |
| **Prefix** | `Basic `（注意 "Basic" 后有一个空格）|

**(d) 预期结果**

创建成功后生成一个 **Credential Provider ARN**。Gateway 调用 Jira 时会发送请求头 `Authorization: Basic <base64>`。记下该 ARN，Target 4 会引用它。

![截图占位: step2-2-apikey-provider.png](images/step2-2-apikey-provider.png)

---

## 步骤 3: 添加 Target 1 — LangGraph Agent

LangGraph Agent 是自托管 HTTP 服务，使用 **HTTP Passthrough** 直通模式。

**(a) 导航路径**

AgentCore → Gateways → 打开 `<project>-gateway` → **Targets** 选项卡 → **Add target**（添加目标）。

**(b) 字段填写**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `langgraph-agent` |
| **Target type** | **HTTP Passthrough** |
| **Protocol type** | **CUSTOM**（HTTP 直通模式必须使用 `protocolType = CUSTOM`）|
| **Endpoint** | LangGraph 服务的 **HTTPS** ALB URL，来自 `deploy-outputs.env` 的 `LANGGRAPH_ENDPOINT`（例如 `https://langgraph.<your-domain>`）。**必须是 `https://`。** |
| **Outbound authentication** | 选择 **OAuth**，并引用[步骤 2.1](#21-oauth-credential-provider) 创建的 OAuth 凭证提供者 (`<project>-cognito-oauth`)，作用域填 `<project>/invoke`。|

**(c) 预期结果**

Target 创建后进入同步流程，状态最终变为 **`READY`**。若使用了 HTTP 直通，Gateway 会将请求原样透传到该端点，并自动附加从 OAuth 提供者获取的 `Authorization: Bearer <token>`。

![截图占位: step3-target-langgraph.png](images/step3-target-langgraph.png)

---

## 步骤 4: 添加 Target 2 — Claude Agent

Claude Agent 通过 **OpenAPI 规范**发现工具，Gateway 将其转换为 MCP 工具。

**(a) 导航路径**

同一 Gateway → **Targets** → **Add target**。

**(b) 字段填写**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `claude-agent` |
| **Target type** | **MCP** → **OpenAPI Schema**（OpenAPI 规范）|
| **Schema source** | **S3 URI**，填 `openapi.yaml` 的 S3 地址，来自 `deploy-outputs.env` 的 `CLAUDE_OPENAPI_S3_URI`（例如 `s3://<project>-gateway-specs-<accountId>/claude-agent/openapi.yaml`）。|
| **Outbound authentication** | 选择 **OAuth**，引用[步骤 2.1](#21-oauth-credential-provider) 的 OAuth 提供者，作用域 `<project>/invoke`。|

> Gateway 执行角色必须能对该 S3 对象执行 `s3:GetObject`（见[步骤 1](#步骤-1-创建-agentcore-gateway) 的角色要求），否则规范无法读取，Target 会失败。

**(c) 预期结果**

Gateway 拉取并校验 OpenAPI 规范，将其中定义的操作 (operations) 转换为可发现的 MCP 工具。Target 状态变为 **`READY`**。若规范无效或无法访问，状态会变为 `FAILED`（见[附录 B](#附录-b-常见问题排查)）。

![截图占位: step4-target-claude-openapi.png](images/step4-target-claude-openapi.png)

---

## 步骤 5: 添加 Target 3 — MCP Server

MCP Server 是原生 MCP 服务，直接作为 MCP Server Target 接入。

**(a) 导航路径**

同一 Gateway → **Targets** → **Add target**。

**(b) 字段填写**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `mcp-server` |
| **Target type** | **MCP** → **MCP Server**（原生 MCP 服务器）|
| **Server URL** | MCP 服务的 **HTTPS** ALB URL **加上 `/mcp` 路径**，来自 `deploy-outputs.env` 的 `MCP_ENDPOINT`（例如 `https://mcp.<your-domain>/mcp`）。**必须是 `https://`。** |
| **Outbound authentication** | 选择 **OAuth**，引用[步骤 2.1](#21-oauth-credential-provider) 的 OAuth 提供者，作用域 `<project>/invoke`。|

**(c) 预期结果**

Gateway 与该 MCP 服务握手，发现其暴露的工具并聚合进网关。Target 状态变为 **`READY`**。

![截图占位: step5-target-mcp-server.png](images/step5-target-mcp-server.png)

---

## 步骤 6: 添加 Target 4 — Jira Cloud

Jira Cloud 是外部 SaaS，使用 **HTTP Passthrough** + API Key（Basic 认证）。

**(a) 导航路径**

同一 Gateway → **Targets** → **Add target**。

**(b) 字段填写**

| 字段 (Field) | 值 (Value) / 来源 |
|--------------|-------------------|
| **Name** | `jira-cloud` |
| **Target type** | **HTTP Passthrough** |
| **Protocol type** | **CUSTOM** |
| **Endpoint** | `https://your-domain.atlassian.net/rest/api/3`（将 `your-domain` 替换为你的 Atlassian 站点；来自 `config.env` 的 `JIRA_BASE_URL`）。**必须是 `https://`。** |
| **Outbound authentication** | 选择 **API Key**，引用[步骤 2.2](#22-api-key-credential-provider用于-jira) 创建的 API Key 提供者 (`<project>-jira-apikey`)。位置 **HEADER**、参数名 `Authorization`、前缀 `Basic `。|

> 若未配置 Jira（`JIRA_BASE_URL` 为空），可跳过此 Target；`setup-gateway.py` 也会自动跳过它。

**(c) 预期结果**

Target 状态变为 **`READY`**。Gateway 向 Jira REST API 透传请求，并自动附加 `Authorization: Basic <base64(email:api_token)>` 请求头。

![截图占位: step6-target-jira.png](images/step6-target-jira.png)

---

## 步骤 7: 同步 Targets 并验证

**(a) 触发同步 (Sync)**

1. 导航：AgentCore → Gateways → `<project>-gateway` → **Targets**。
2. 若某个 Target 仍处于 `CREATING` / `UPDATING`，或你修改了配置，点击该 Target 的 **Sync**（同步）按钮以重新发现工具。
3. 等待每个 Target 状态变为 **`READY`**。（失败态包括 `FAILED`、`CREATE_FAILED`、`SYNCHRONIZE_UNSUCCESSFUL`、`UPDATE_UNSUCCESSFUL`。）

**(b) 验证工具发现 (Tool Discovery)**

Target 全部就绪后，在 Gateway 详情页应能看到从 4 个 Target 聚合而来的工具列表。启用了语义搜索时还可对工具做检索。

**(c) 命令行等价验证 (CLI Equivalent)**

列出网关的所有 Target 及其状态：

```bash
aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier <gateway-id> \
  --region ap-northeast-1
```

本项目还提供 `gateway/validate.py`，它会用 gateway-client 获取入站令牌，调用网关 MCP 端点执行 `initialize` + `tools/list`，并打印发现的工具：

```bash
source deploy-outputs.env
venv/bin/python gateway/validate.py
```

**预期结果**：4 个 Target 均为 `READY`，`tools/list` 返回非空的工具集合（分别来自 langgraph-agent、claude-agent、mcp-server、jira-cloud）。

![截图占位: step7-targets-ready.png](images/step7-targets-ready.png)
![截图占位: step7-tools-discovered.png](images/step7-tools-discovered.png)

---

## 步骤 8: 在 Amazon Quick / Kiro 中连接 Gateway

MCP 客户端（Amazon Quick 或 Kiro）通过网关的 MCP 端点连接，并需携带由 **gateway-client** 签发的 Bearer 令牌（入站鉴权）。

**(a) 获取入站访问令牌 (Access Token)**

使用 gateway-client 以 **client_credentials** 授权类型向 Cognito 令牌端点换取令牌：

```bash
curl -s -X POST \
  "https://<domain>.auth.ap-northeast-1.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "<gatewayClientId>:<gatewayClientSecret>" \
  -d "grant_type=client_credentials&scope=<project>/invoke"
```

- 令牌 URL 来自栈输出 `CognitoTokenUrl`。
- `<gatewayClientId>` 来自栈输出 `GatewayClientId`。
- `<gatewayClientSecret>` 通过 `aws cognito-idp describe-user-pool-client` 获取（**切勿落盘**）。
- 响应中的 `access_token` 即为 Bearer 令牌。

**(b) 配置 MCP 连接 (MCP Connection)**

在 Amazon Quick / Kiro 的 MCP 服务器配置中：

| 配置项 | 值 |
|--------|-----|
| **Server URL / MCP endpoint** | [步骤 1](#步骤-1-创建-agentcore-gateway) 记下的网关 MCP 端点 URL（`deploy-outputs.env` 的 `GATEWAY_URL`）|
| **Transport** | Streamable HTTP（MCP over HTTP）|
| **Authorization header** | `Bearer <access_token>`（来自上一步）|

**(c) 测试一次工具调用 (Test a Tool Call)**

在客户端里列出工具，应能看到聚合自 4 个 Target 的工具；随后调用其中任意一个（例如 LangGraph agent 的对话工具或 Jira 的查询工具），确认返回正常。

**(d) 预期结果**

客户端成功列出并调用网关聚合的工具。若返回 401，通常是令牌的受众 (audience) 或 `client_id` 与网关 `allowedClients` 不匹配（见[附录 B](#附录-b-常见问题排查)）。

![截图占位: step8-mcp-connection.png](images/step8-mcp-connection.png)
![截图占位: step8-tool-call-result.png](images/step8-tool-call-result.png)

---

## 附录 A: 获取所需值的速查表

下表把每个所需值映射到产出它的 **CDK 栈输出名** 或 **CLI 命令**。

- `<project>-cognito` 栈的输出可通过 `deploy.sh` 写入的 `cdk-outputs.json` 读取，或用 CLI 直接查询。
- `deploy.sh` 运行结束后会把解析好的端点/标识符写入 `deploy-outputs.env`。

| 所需值 | 来源（CDK 输出名 / CLI 命令）|
|--------|------------------------------|
| **User Pool ID** | `<project>-cognito` 栈输出 `UserPoolId`（`deploy-outputs.env` → `COGNITO_USER_POOL_ID`）|
| **Discovery URL**（入站/出站鉴权）| `<project>-cognito` 栈输出 `CognitoDiscoveryUrl`（`COGNITO_DISCOVERY_URL`）|
| **Token URL**（令牌端点）| `<project>-cognito` 栈输出 `CognitoTokenUrl`（`COGNITO_TOKEN_URL`）|
| **Scope**（作用域）| `<project>-cognito` 栈输出 `CognitoScope`，值为 `<project>/invoke`（`COGNITO_SCOPE`）|
| **service-client ID** | `<project>-cognito` 栈输出 `ServiceClientId`（`COGNITO_SERVICE_CLIENT_ID`）|
| **service-client secret** | CLI（见下方命令），不作为 CloudFormation 输出 |
| **gateway-client ID** | `<project>-cognito` 栈输出 `GatewayClientId`（`COGNITO_GATEWAY_CLIENT_ID`）|
| **gateway-client secret** | CLI（见下方命令），不作为 CloudFormation 输出 |
| **Gateway execution role ARN** | `<project>-cognito` 栈输出 `GatewayRoleArn`（`GATEWAY_ROLE_ARN`）|
| **LangGraph ALB 端点** | `deploy-outputs.env` → `LANGGRAPH_ENDPOINT` |
| **Claude ALB 端点** | `deploy-outputs.env` → `CLAUDE_ENDPOINT` |
| **MCP ALB 端点**（含 `/mcp`）| `deploy-outputs.env` → `MCP_ENDPOINT` |
| **Claude OpenAPI S3 URI** | `deploy-outputs.env` → `CLAUDE_OPENAPI_S3_URI` |
| **Gateway MCP 端点 URL** | `deploy-outputs.env` → `GATEWAY_URL`（或 `gateway/generated-gateway.json` 的 `gatewayUrl`）|

**读取某个 CDK 栈输出（从 `cdk-outputs.json`）：**

```bash
jq -r '."<project>-cognito".UserPoolId' cdk-outputs.json
```

**直接用 CLI 查询栈输出：**

```bash
aws cloudformation describe-stacks \
  --stack-name <project>-cognito \
  --region ap-northeast-1 \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoTokenUrl'].OutputValue" \
  --output text
```

**获取客户端密钥（service-client / gateway-client），不打印到日志外的地方：**

```bash
# service-client secret
aws cognito-idp describe-user-pool-client \
  --user-pool-id <userPoolId> \
  --client-id <ServiceClientId> \
  --region ap-northeast-1 \
  --query 'UserPoolClient.ClientSecret' --output text

# gateway-client secret
aws cognito-idp describe-user-pool-client \
  --user-pool-id <userPoolId> \
  --client-id <GatewayClientId> \
  --region ap-northeast-1 \
  --query 'UserPoolClient.ClientSecret' --output text
```

> **安全提示**：客户端密钥与 Jira API token 都属于敏感凭证。仅在填写控制台表单时临时使用，**不要写入 `config.env`、代码库或任何日志**。Jira API token 应存入 Secrets Manager（`<project>/jira-api-token`）。

---

## 附录 B: 常见问题排查

**1. Target 卡在 `FAILED` / `CREATE_FAILED` / `SYNCHRONIZE_UNSUCCESSFUL`**

- 检查 Gateway **执行角色**是否具备读取 S3 规范 (`s3:GetObject`) 以及使用 Token Vault 凭证提供者 (`bedrock-agentcore:GetResourceOauth2Token` 等) 的权限。
- 确认 Target 端点可从 AWS 侧访问，且证书链有效（自签名或过期证书会导致 TLS 握手失败）。
- 在 Target 详情页查看失败原因/状态说明；修复后点击 **Sync** 重新同步。

**2. 服务返回 401（令牌受众 / client_id 不匹配）**

- **入站 401**（客户端 → 网关）：确认令牌由 **gateway-client** 签发，且该 client_id 在网关的 **Allowed clients** 列表中。
- **出站 401**（网关 → 服务 1/2/3）：确认 OAuth 凭证提供者使用的是 **service-client** 的 ID/密钥，且服务端校验的正是该 client_id；作用域必须是 `<project>/invoke`。
- 确认 Discovery URL 指向正确的用户池，令牌未过期。

**3. 端点必须是 HTTPS**

- 所有 Target 端点（LangGraph、Claude、MCP、Jira）必须以 `https://` 开头。仅 HTTP 的 ALB 无法创建 Target。请为 ALB 挂载 ACM 证书并配置 HTTPS(443) 监听器（设置 `ACM_CERTIFICATE_ARN` 与 `BASE_DOMAIN`）。

**4. OpenAPI 规范校验错误**

- 确认 S3 URI 正确、对象存在，且执行角色可 `s3:GetObject`。
- 确认规范为合法的 OpenAPI 文档，且其中的 `${CLAUDE_AGENT_HOST}`、`${COGNITO_DOMAIN}` 占位符在上传前已替换为真实值。
- 规范中的 `servers` 主机需与实际可达的 HTTPS 端点一致。

**5. `tools/list` 返回空**

- 说明尚无 Target 就绪或工具未被发现。回到[步骤 7](#步骤-7-同步-targets-并验证) 逐个检查 Target 状态并触发 **Sync**。
