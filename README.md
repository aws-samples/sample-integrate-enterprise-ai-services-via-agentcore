# 通过 Amazon Bedrock AgentCore Gateway 接入企业自建 AI 服务

将四类典型的自建 AI 服务统一挂到一个 AgentCore Gateway 后面,任何 MCP 客户端
(Amazon Quick、Kiro 等)只需连接一个端点,即可看到统一、受 OAuth 保护的工具目录。
提供一键部署脚本与手动 Console 操作指南两条路径。

## 架构

```
                     Amazon EKS 集群 (ap-northeast-1)
   ┌──────────────────────────────────────────────────────────────┐
   │  服务 1              服务 2               服务 3               │
   │  LangGraph Agent    Claude Agent SDK      MCP Server           │
   │  自定义 REST         OpenAPI 3.0           Streamable HTTP      │
   │       │                   │                     │              │
   │       └─── 每服务一个内网 ALB (HTTP:80, 私有子网) ────────────│
   └──────────────────────────────┬───────────────────────────────┘
                                   │  VPC Link (私有集成)
                    ┌──────────────┴───────────────────────────────┐
                    │  Amazon API Gateway HTTP API (每服务一个)      │
                    │  execute-api HTTPS (AWS 托管证书, 无需域名)    │
                    │  + Cognito JWT 授权器 (issuer + audience)      │
                    └──────────────┬───────────────────────────────┘
                                   │  (https:// execute-api 端点)
                    ┌──────────────┴───────────────┐
                    │      AgentCore Gateway         │
                    │  T1 OpenAPI → MCP   ← 服务 1   │
                    │  T2 OpenAPI → MCP   ← 服务 2   │
                    │  T3 MCP Server      ← 服务 3   │
                    │  T4 OpenAPI → MCP   ← Jira 云  │
                    └──────────────┬───────────────┘
                                   │  (MCP, 入站 OAuth/JWT)
                    ┌──────────────┴───────────────┐
                    │   Amazon Quick / Kiro (MCP)    │
                    └────────────────────────────────┘
```

## 四个示例服务

聚合型 MCP 网关会把每个 Target 暴露成 MCP 工具,因此每个 Target 都必须带工具 schema
(OpenAPI、原生 MCP 或 Lambda)。HTTP passthrough 只能挂到"无协议类型"的网关上、
不会被聚合成工具目录,故本方案统一采用 OpenAPI / 原生 MCP 两种 Target。

1. **LangGraph Agent** (`services/langgraph-agent`)——ReAct agent(计算器、天气、
   知识库、时间工具),自定义 REST API(`/api/v1/chat`、`/api/v1/tasks`)。附带
   `openapi.yaml`,作为 **OpenAPI → MCP** Target 接入。调用**真实 Bedrock** 上的 Claude。
2. **Claude Agent SDK** (`services/claude-agent`)——客服 agent(订单、退货、推荐),
   完整 `openapi.yaml`,作为 **OpenAPI → MCP** Target。使用 Anthropic Claude Agent
   SDK 走 Bedrock(`CLAUDE_CODE_USE_BEDROCK=1`)。
3. **MCP Server** (`services/mcp-server`)——企业知识库**原生 MCP 服务**
   (Streamable HTTP,`/mcp`),4 个工具,作为 **MCP Server** Target。
4. **Jira Cloud** (`services/jira-integration`)——外部 SaaS,作为 **OpenAPI → MCP**
   Target,出站用 **API-key**(Basic 认证)。无需部署。

服务 1–3 位于私有子网、仅 HTTP:80,由各自的 API Gateway HTTP API 在边缘用 **Cognito
JWT 授权器**校验入站 token。Pod 内默认 `AUTH_DISABLED=true`;设为 `false` 可在 Pod
内再做一次 Cognito JWT 校验(纵深防御)。网关出站对服务 1–3 用 Cognito OAuth 凭证
提供者,对 Jira 用 API-key 提供者。

## 前置条件

- AWS 账号,具备 EKS、ECR、Cognito、IAM、S3、Secrets Manager、API Gateway、
  Bedrock AgentCore 权限,并在 `ap-northeast-1` 开通 `config.env` 中 Claude 模型的
  **Bedrock 模型访问权限**。
- **无需自有域名或 ACM 证书**——TLS 由 API Gateway 的 execute-api 端点自动提供。
- 本地工具:**AWS CLI v2**、**AWS CDK v2**(`npm i -g aws-cdk`)、**kubectl**、
  **jq**、**envsubst**(gettext)、**zip**、**Python 3.10+**。**无需本地 Docker**——
  镜像在 AWS CodeBuild 上原生 `linux/amd64` 构建,任意架构的机器都能部署。

## 快速开始

```bash
# 1. 创建 Python 环境
python3 -m venv venv
./venv/bin/pip install -r infrastructure/cdk/requirements.txt -r gateway/requirements.txt

# 2. 从模板复制配置并编辑(默认区域 ap-northeast-1)
#    config.env 已被 git 忽略;config.example.env 是提交进仓库的模板。
cp config.example.env config.env
$EDITOR config.env          # 设置 Bedrock 模型,可选填 Jira;无需域名/证书

# 3. 一键部署(EKS 预置约 15-25 分钟)
./deploy.sh

# 4. 校验网关连通性与工具发现
source deploy-outputs.env
./venv/bin/python gateway/validate.py

# 5. 销毁全部资源
./destroy.sh
```

`deploy.sh` 会把解析出的端点与标识写入 `deploy-outputs.env`(git 忽略),网关详情写入
`gateway/generated-gateway.json`。

## TLS 由 API Gateway 自动提供

AgentCore Gateway 的 Target **必须是带公信证书的 `https://` 端点**。本方案无需自有
域名和 ACM 证书:三个 EKS 服务各由一个 **API Gateway HTTP API** 承载,其内置端点

```
https://{api-id}.execute-api.ap-northeast-1.amazonaws.com
```

自带 **AWS 托管的公信证书**。使用 **`$default` 阶段**(自动部署),execute-api URL 无
阶段路径前缀,直接映射到服务根路径。

- **内网 ALB,仅 HTTP。** 服务 ALB 位于私有子网、只跑 HTTP:80,不对外暴露;
  API Gateway 通过 CDK 创建的 **VPC Link** 私有集成访问它们。
- **边缘鉴权。** 每个 HTTP API 配 **Cognito JWT 授权器**(issuer = 本 Demo 的
  Cognito 用户池;audience = [service client id, gateway client id];要求 scope
  `<project>/invoke`)。Cognito `client_credentials` token 只有 `client_id`、无 `aud`,
  API Gateway 在缺少 `aud` 时会用 `client_id` 匹配 audience。
- **`gateway/setup-apigateway.py`** 在内网 ALB 就绪后创建这三个 HTTP API,并把各自
  的 execute-api 端点写入 `gateway/generated-apigateway.json`;
  **`gateway/teardown-apigateway.py`** 在 `cdk destroy` 删除 VPC Link 前先删它们。

> **注意:HTTP API 集成有 30 秒硬超时。** Demo 工具均为秒级响应,不受影响;但真正的
> 长连接 MCP 流(SSE)会超时,需改用其它承载方式(如 NLB + ACM),而非 HTTP API。

> **生产环境说明。** 本方案用「内网 ALB + API Gateway」的组合,只是为了在**没有自有
> 域名、没有 ACM 证书**的前提下,仍能给 AgentCore Gateway 提供合法的 `https://`
> Target 端点。真实环境中,自建服务通常直接用**公网 ALB + ACM 证书 + 自有域名**
> (在 `config.env` 里配置证书/域名,把 Ingress 改回 `internet-facing` + HTTPS
> 监听器),AgentCore Gateway 直接指向该域名即可,无需 API Gateway 与 VPC Link 这
> 一层。是否保留 API Gateway 层,取决于你是否需要它额外提供的限流、WAF、集中鉴权
> 等能力。

## 配置

所有环境相关值都在 **`config.env`**:区域、命名(含 `PROJECT_SHORT`——内网 ALB 名
上限 32 字符时用的短前缀)、EKS 规格、Bedrock 模型 id、Cognito 域、网关选项、
`AUTH_DISABLED`(默认 `"true"`)、Jira。不含任何域名或证书设置。`config.env` 由
提交进仓库的 **`config.example.env`** 复制而来(`cp config.example.env config.env`),
并被 **git 忽略**,填入的账号 id、Jira 邮箱/URL 等都不会进入仓库。真正的密钥
(Cognito client secret、Jira API token)存于 Cognito / Secrets Manager,部署时解析,
不落盘、不入日志。

## 目录结构

```
sample-integrate-enterprise-ai-services-with-agentcore/
├── README.md
├── config.example.env             # 配置模板(已提交)——复制为 config.env
├── config.env                     # 你的副本(git 忽略)——唯一配置来源
├── deploy.sh / destroy.sh         # 一键生命周期脚本
├── infrastructure/cdk/            # CDK(Python):ECR、CodeBuild、Cognito、EKS 栈
├── services/
│   ├── langgraph-agent/           # 服务 1(+ openapi.yaml)
│   ├── claude-agent/              # 服务 2(+ openapi.yaml)
│   ├── mcp-server/                # 服务 3
│   └── jira-integration/          # 服务 4 配置 + Jira OpenAPI 子集
├── gateway/
│   ├── gateway-config.yaml        # 声明式网关 + Target 定义
│   ├── setup-apigateway.py        # 为每个内网 ALB 前置 HTTP API
│   ├── teardown-apigateway.py     # 删除 HTTP API(先于 VPC Link)
│   ├── setup-gateway.py           # 创建网关 + 4 个 Target
│   ├── teardown-gateway.py        # 删除网关资源
│   └── validate.py                # 连通性 + 工具发现校验
├── scripts/render-k8s.sh          # k8s 清单的 ${...} 占位替换
└── docs/console-setup-guide.md    # 手动 Console 操作指南
```

## 部署流程

1. **CDK** 创建 ECR 仓库、CodeBuild 构建项目、Cognito 用户池(资源服务器 +
   service/gateway 应用客户端 + 网关执行角色)、EKS 集群(含 AWS Load Balancer
   Controller 与开通 Bedrock 的 IRSA 账号),以及让 API Gateway 私有访问内网 ALB 的
   **VPC Link**。
2. **deploy.sh** 在 CodeBuild 上构建并推送三个镜像,渲染并应用 k8s 清单,等待
   **内网 ALB** 就绪,运行 **setup-apigateway.py**,上传 OpenAPI 规范到 S3,再运行
   **setup-gateway.py**。
3. **setup-apigateway.py** 为每个内网 ALB 前置一个 HTTP API(execute-api HTTPS +
   Cognito JWT 授权器 + VPC Link 私有集成),端点写入 `generated-apigateway.json`。
4. **setup-gateway.py** 创建出站凭证提供者、网关(MCP 协议、Cognito JWT 入站鉴权)
   与四个 Target,并打印网关 MCP 端点 URL。
5. 将 **Amazon Quick / Kiro** 指向该 MCP 端点(用 gateway 应用客户端签发的 bearer
   token),即可通过一个连接使用全部服务的工具。

手动 Console 路径见 **[docs/console-setup-guide.md](docs/console-setup-guide.md)**。

## 费用提示

EKS(控制面 + 2× `t3.large` 节点)、3 个内网 ALB、NAT 网关、真实 Bedrock 推理都会
计费;API Gateway 按请求计费,Demo 量级可忽略。用完请 `./destroy.sh`。本项目为演示,
非生产参考实现。

## 安全

- 入站/出站全程 Cognito OAuth 2.0,无硬编码凭证。
- 密钥存于 Cognito / AWS Secrets Manager,运行时解析。
- Pod 以非 root 运行;Bedrock 访问经 IRSA 授权,不用静态密钥。
