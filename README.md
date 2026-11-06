# Production Customer Support AI Agent — Amazon Bedrock AgentCore

A customer support agent for an e-commerce store, built with the **Strands Agents SDK** and deployed to **Amazon Bedrock AgentCore Runtime**. It combines RAG over a product/policy knowledge base, transactional tools exposed through **AgentCore Gateway (MCP)**, short- and long-term **memory**, exact arithmetic in the **Code Interpreter** sandbox, and live web lookups with the **Browser** tool.

> Built as the Course 2 project of the Udacity × AWS *Agent Engineer* Nanodegree.

## Architecture

```
Customer ──▶ AgentCore Runtime (Strands Agent, Amazon Nova 2 Lite)
                │
                ├── search_knowledge_base ──▶ Bedrock Knowledge Base (products, returns, loyalty tiers)
                ├── Gateway tools (MCP / Streamable HTTP)
                │      ├── order_tracker     ──▶ Lambda (GET /orders/{id}, /customers/{id}/orders)
                │      └── refund_processor  ──▶ Lambda (create refund, return label)
                ├── calculate_loyalty_discount ──▶ AgentCore Code Interpreter (sandboxed Python)
                ├── browser ──▶ AgentCore Browser (live page title / headings)
                └── MemoryHook ──▶ AgentCore Memory (preferences + conversation summaries)
```

## Key design decisions

- **Transactions go through tools, not the LLM.** Order lookups and refunds are Lambda functions behind AgentCore Gateway, so inputs are schema-validated and the model never touches data stores directly.
- **No generative arithmetic.** Loyalty-point redemption and tier discounts are computed in the Code Interpreter sandbox; the system prompt requires the agent to report the tool's numbers verbatim.
- **Memory as a hook, not a prompt hack.** `MemoryHook` retrieves customer context when a message arrives and saves the interaction after each invocation, using AgentCore Memory namespaces per actor.
- **Graceful degradation.** Every Gateway tool is wrapped with a timeout and converts HTTP errors / connection failures into a clear message with a safe next step instead of a stack trace. If the Gateway is unreachable at startup, the agent still runs with a `gateway_service_notice` tool.

See [docs/reflection.md](docs/reflection.md) for the design rationale, the event-loop issue solved when running the Browser tool inside the ASGI entrypoint, and production hardening notes (IAM/OAuth gateway auth, egress allowlisting, CloudWatch alarms).

## Project structure

```
main.py                     # Agent, tools, memory hook, AgentCore entrypoint
lambda/order_tracker.py     # Order status / tracking Lambda
lambda/refund_processor.py  # Refund + return label Lambda
lambda/lambda_schema        # Tool schema registered on the Gateway
product_catalog.txt         # Source document for the Knowledge Base
.bedrock_agentcore.yaml     # AgentCore deployment config (account ID redacted)
docs/                       # Reflection + test screenshots
```

## Running it

Requires an AWS account with Bedrock model access (Amazon Nova), Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
# local run
uv run main.py
# deploy to AgentCore Runtime
agentcore configure --entrypoint main.py
agentcore launch
agentcore invoke '{"prompt": "Where is my order ORD-1001?"}'
```

Set `KB_ID`, `MEMORY_ID` and the Gateway URL in `main.py` to the resources in your account, and replace `<AWS_ACCOUNT_ID>` in `.bedrock_agentcore.yaml`.

## Functional tests

Six end-to-end scenarios were verified against the deployed agent (screenshots in [docs/screenshots](docs/screenshots)):

| # | Scenario | Capability exercised |
|---|----------|----------------------|
| 1 | Track an order | Gateway → Lambda (MCP) |
| 2 | Request a refund | Gateway → Lambda (MCP) |
| 3 | Ask about loyalty tier benefits | Knowledge Base RAG |
| 4 | Introduce yourself, then recall in a new session | Long-term memory |
| 5 | Compute discount for points + tier | Code Interpreter |
| 6 | Fetch a live page title | Browser tool |

Runtime errors are monitored with CloudWatch metric filters and alarms on the AgentCore log streams.
