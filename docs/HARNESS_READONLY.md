# Harness / MCP 接入

本分支实现一个独立、只读、按物业范围过滤的 stdio MCP server；无需 OpenAI 或 DeepSeek API key 即可测试。它不包含模型调用，也不修改任何现有 MCP 配置。

四个工具：get_property_facts、list_arrivals、list_overdue_tasks、get_reconciliation。结果提供证据 ID；不返回门码、门户令牌、客人身份、邮箱或 Wi-Fi 密钥。房源描述与 house rules 仍是业务输入，Agent 必须将其视为不可信数据，不能按其中的指令执行。

启动前必须提供 OPS_TOOL_DATABASE_URL 和 OPS_TOOL_PROPERTY_IDS。SQLite 要求已存在的绝对文件路径并强制 mode=ro；PostgreSQL 会话 SET TRANSACTION READ ONLY。建议另设只读数据库账号。属性范围为空时拒绝启动。不要把生产密钥加入 YAML。

基于官方当前 MCP client README 的配置草案，参数以本地环境变量传入：

```yaml
- id: oceano-ops-readonly
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: oceano_ops
    transport: stdio
    command: !!js process.env.OPS_TOOL_PYTHON
    args: ['-m', 'app.operations.mcp_server']
    cwd: !!js process.env.OPS_TOOL_REPO
    env:
      OPS_TOOL_DATABASE_URL: !!js process.env.OPS_TOOL_DATABASE_URL
      OPS_TOOL_PROPERTY_IDS: !!js process.env.OPS_TOOL_PROPERTY_IDS
    failOnStartupError: true
    toolCallTimeoutMs: 20000
```

官方参考：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md

本配置尚未被实际 Harness 实例加载。MCP initialize/tools/list/tools/call 已通过本地协议测试；不能据此声称已完成 Harness 端到端验证。当前 CLI 提供基础 MCP 工具子集，不支持 resources、prompts 或 HTTP transport。配置只启用业务读取；REA 连接用单独条目，不把它混入客人运营权限。当前没有指定授权 Hostaway artifact，未运行反编译。

Harness 官方声明仍为 developer preview，部署前固定确切版本和依赖，并在合成数据环境验收。当前未安装或运行 Harness，不把其存在当完成前提。模型供应商/预算与数据目的地确定后再接模型；本轮没有上传任何经营数据。
