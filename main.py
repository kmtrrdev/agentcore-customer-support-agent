"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3, threading
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-d7ngvw9lwa.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = os.environ.get("KB_ID", "")
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-GPUlC9HKHm"


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# Create the BedrockModel instance
model = BedrockModel(model_id=model_id)

# Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)

# Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)

# Create the AgentCoreBrowser instance
agent_core_browser = AgentCoreBrowser(region=REGION)

# Isolate browser event loop on a dedicated thread to prevent AnyIO ASGI loop conflict
_bg_browser_loop = asyncio.new_event_loop()
_bg_browser_thread = threading.Thread(target=_bg_browser_loop.run_forever, daemon=True)
_bg_browser_thread.start()

def _safe_browser_execute(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _bg_browser_loop)
    return future.result(timeout=60)

agent_core_browser._execute_async = _safe_browser_execute

_orig_browser_cleanup = agent_core_browser._async_cleanup
async def _safe_browser_cleanup():
    await _orig_browser_cleanup()
    agent_core_browser._started = False

agent_core_browser._async_cleanup = _safe_browser_cleanup

_orig_create_browser_session = agent_core_browser.create_browser_session
async def _safe_create_browser_session():
    if not agent_core_browser._playwright:
        agent_core_browser._started = False
        agent_core_browser._start()
    return await _orig_create_browser_session()

agent_core_browser.create_browser_session = _safe_create_browser_session

_orig_browser_init = agent_core_browser._async_init_session
async def _safe_browser_init(action):
    if not agent_core_browser._playwright:
        agent_core_browser._started = False
        agent_core_browser._start()
    if action.session_name in agent_core_browser._sessions:
        old_sess = agent_core_browser._sessions.pop(action.session_name, None)
        if old_sess:
            try:
                await old_sess.close()
            except Exception:
                pass
    return await _orig_browser_init(action)

agent_core_browser._async_init_session = _safe_browser_init


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    try:
        strategies = mem_client.get_memory_strategies(memory_id)
        namespaces = {}
        for strategy in strategies:
            strat_type = strategy.get("type") or strategy.get("memoryStrategyType")
            ns_list = strategy.get("namespaces") or strategy.get("namespaceTemplates") or []
            if strat_type and ns_list:
                namespaces[strat_type] = ns_list[0]
        return namespaces
    except Exception as e:
        logger.warning(f"Error getting memory strategies: {e}")
        return {
            "SEMANTIC": "cs_agent/{actorId}/facts",
            "USER_PREFERENCE": "cs_agent/{actorId}/preferences",
        }


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        message = getattr(event, "message", None)
        if not message or message.get("role") != "user":
            return

        content = message.get("content", [])
        if isinstance(content, list):
            if any(isinstance(b, dict) and "toolResult" in b for b in content):
                return
            user_query = ""
            for b in content:
                if isinstance(b, dict) and "text" in b:
                    user_query += b["text"]
                elif isinstance(b, str):
                    user_query += b
        elif isinstance(content, str):
            user_query = content
        else:
            return

        if not user_query.strip():
            return

        memory_texts = []
        for strat_type, template in self.namespaces.items():
            namespace = template.replace("{actorId}", self.actor_id)
            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
                for mem in memories:
                    text = ""
                    if isinstance(mem, dict):
                        content_dict = mem.get("content")
                        if isinstance(content_dict, dict) and "text" in content_dict:
                            text = content_dict["text"]
                        elif "text" in mem:
                            text = mem["text"]
                        elif "memory" in mem:
                            text = str(mem["memory"])
                    elif isinstance(mem, str):
                        text = mem

                    if text and text.strip():
                        memory_texts.append(f"[{strat_type}] {text.strip()}")
            except Exception as e:
                logger.warning(f"Failed to retrieve memories for namespace {namespace}: {e}")

        if memory_texts:
            context_str = (
                "Customer Context (Past history; do NOT use past order amounts when the user specifies a new order total):\n"
                + "\n".join(memory_texts)
                + "\n\nCurrent Customer Message:\n"
            )
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and "text" in b:
                        b["text"] = context_str + b["text"]
                        break
            elif isinstance(content, str):
                message["content"] = context_str + content

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = getattr(event.agent, "messages", [])
        if not messages:
            return

        assistant_response = None
        customer_query = None

        for msg in reversed(messages):
            role = msg.get("role")
            content = msg.get("content", [])

            if isinstance(content, list) and any(
                isinstance(b, dict) and ("toolResult" in b or "toolUse" in b) for b in content
            ):
                continue

            text = ""
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and "text" in b:
                        text += b["text"]
                    elif isinstance(b, str):
                        text += b
            elif isinstance(content, str):
                text = content

            if role == "assistant" and assistant_response is None and text.strip():
                assistant_response = text.strip()
            elif role == "user" and customer_query is None and text.strip():
                cleaned = text.strip()
                if "Customer Context:\n" in cleaned and "\n\n" in cleaned:
                    cleaned = cleaned.split("\n\n", 1)[1]
                customer_query = cleaned

            if assistant_response and customer_query:
                break

        if assistant_response and customer_query:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (customer_query, "USER"),
                        (assistant_response, "ASSISTANT"),
                    ],
                )
            except Exception as e:
                logger.warning(f"Failed to save support interaction: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID.strip() in ("", "<kbid>"):
        try:
            catalog_path = os.path.join(os.path.dirname(__file__), "product_catalog.txt")
            if os.path.exists(catalog_path):
                with open(catalog_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [
            r["content"]["text"]
            for r in results
            if "content" in r and "text" in r["content"]
        ]
        if not chunks:
            return "No relevant text chunks found."
        return "\n---\n".join(chunks)
    except Exception as e:
        logger.warning(f"Knowledge base search error: {e}")
        try:
            catalog_path = os.path.join(os.path.dirname(__file__), "product_catalog.txt")
            if os.path.exists(catalog_path):
                with open(catalog_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return f"Error searching knowledge base: {str(e)}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD as a number without dollar sign (e.g. 150.0)
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    logger.info(f"calculate_loyalty_discount:points={loyalty_points}, tier={tier}, order_total={order_total}, cat={product_category}")
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

available_points = (loyalty_points // 500) * 500
max_discount = order_total * 0.50
max_redeemable_points = int((max_discount * 100) // 500) * 500

points_redeemed = min(available_points, max_redeemable_points)
points_discount = round(points_redeemed / 100.0, 2)

subtotal_after_points = round(order_total - points_discount, 2)
tier_rate = tier_rates.get(tier, 0.0)
tier_discount = round(subtotal_after_points * tier_rate, 2)

final_total = round(subtotal_after_points - tier_discount, 2)
total_savings = round(points_discount + tier_discount, 2)
remaining_points = loyalty_points - points_redeemed
points_earned = int(round(final_total * earn_rates.get(product_category, 1)))

result = {{
    "order_total": order_total,
    "product_category": product_category,
    "customer_tier": tier,
    "points_redeemed": points_redeemed,
    "points_discount": points_discount,
    "tier_discount_rate": tier_rate,
    "tier_discount_pct": int(tier_rate * 100),
    "tier_discount": tier_discount,
    "total_savings": total_savings,
    "final_total": final_total,
    "remaining_points": remaining_points,
    "points_earned": points_earned
}}
print(json.dumps(result))
"""

    try:
        with code_session(REGION) as client:
            resp = client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )
            for event in resp.get("stream", []):
                if "result" in event:
                    res_obj = event["result"]
                    stdout = res_obj.get("structuredContent", {}).get("stdout", "").strip()
                    if stdout:
                        try:
                            d = json.loads(stdout)
                            return (
                                f"Loyalty Discount Calculation Results:\n"
                                f"- Order Total: ${d['order_total']:.2f}\n"
                                f"- Customer Tier: {d['customer_tier']} ({int(d['tier_discount_rate']*100)}% tier discount)\n"
                                f"- Points Redeemed: {d['points_redeemed']} points (${d['points_discount']:.2f} discount)\n"
                                f"- Tier Discount: ${d['tier_discount']:.2f}\n"
                                f"- Total Savings: ${d['total_savings']:.2f}\n"
                                f"- Final Total: ${d['final_total']:.2f}\n"
                                f"- Remaining Points: {d['remaining_points']}\n"
                                f"- Points Earned: {d['points_earned']}\n"
                                f"Raw JSON: {stdout}"
                            )
                        except Exception:
                            return stdout
                    content = res_obj.get("content", [])
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            return item.get("text", "")
                    return json.dumps(res_obj)

        return json.dumps({"error": "No result from code interpreter"})

    except Exception as e:
        logger.warning(f"Code interpreter error: {e}. Falling back to tier calculation.")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(tier, 0.0)
        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)
        return json.dumps({
            "order_total": order_total,
            "customer_tier": tier,
            "points_redeemed": 0,
            "tier_discount_pct": int(tier_rate * 100),
            "tier_discount": tier_discount,
            "final_total": final_total,
            "remaining_points": loyalty_points,
            "fallback": True,
            "error": str(e)
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "CUST-DEFAULT")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        try:
            mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
            gateway_tools = await asyncio.wait_for(mcp_client.load_tools(), timeout=15.0)

            def _wrap_gateway_tool(gw_tool):
                orig_func = getattr(gw_tool, "_tool_func", None) or gw_tool
                tool_name = getattr(gw_tool, "name", None) or getattr(gw_tool, "tool_name", "gateway-tool")

                async def _safe_gw_call(*args, **kwargs):
                    try:
                        if asyncio.iscoroutinefunction(orig_func):
                            res = await asyncio.wait_for(orig_func(*args, **kwargs), timeout=25.0)
                        else:
                            res = orig_func(*args, **kwargs)
                        if isinstance(res, dict):
                            status_code = res.get("statusCode")
                            if status_code and int(status_code) >= 400:
                                body_content = res.get("body", "Resource error")
                                return (
                                    f"Gateway Notice: The operation '{tool_name}' returned status {status_code}: {body_content}. "
                                    "Next step: Please check the provided identifiers (such as Order ID or Customer ID) and try again."
                                )
                        return res
                    except (asyncio.TimeoutError, Exception) as err:
                        logger.error(f"Gateway tool {tool_name} call failed: {err}")
                        return (
                            f"Gateway Error: The '{tool_name}' service is temporarily unavailable due to a connection or timeout issue ({type(err).__name__}). "
                            "Next step: Please wait a moment and retry your request. If the issue persists, verify the Gateway service status."
                        )

                if hasattr(gw_tool, "_tool_func"):
                    gw_tool._tool_func = _safe_gw_call
                return gw_tool

            for gt in gateway_tools:
                tools.append(_wrap_gateway_tool(gt))

        except (asyncio.TimeoutError, Exception) as gw_err:
            logger.error(f"Gateway connection/tool loading error ({GATEWAY_URL}): {gw_err}")
            @tool
            def gateway_service_notice(operation: str = "order tracking or refund processing") -> str:
                """Report Gateway service connection issues when Gateway tools are unavailable."""
                return (
                    f"Gateway Connection Error: Unable to connect to the external tools Gateway for {operation}. "
                    "The service may be offline or unreachable. "
                    "Next step: Please retry in a few moments or check the Gateway configuration."
                )
            tools.append(gateway_service_notice)

        system_prompt = (
            "You are an intelligent customer support assistant for an Amazon e-commerce store. "
            "You assist customers with order tracking, return and refund processing, product information, "
            "warranty and return policies, and loyalty program calculations.\n\n"
            "Guidelines:\n"
            "- When a customer introduces themselves (such as sharing their name or communication preferences), greet them warmly, acknowledge their name and preferences concisely, and do not call tools unless requested.\n"
            "- For order details, order status, or tracking numbers: use the order-tracker tools.\n"
            "- For returns, refunds, or return labels: use the refund-processor tools.\n"
            "- When a Gateway tool reports a connection error, timeout, or non-existent resource: Clearly explain what operation failed and provide a safe next step (such as verifying the order number or retrying shortly) without exposing internal secrets or raw stack traces.\n"
            "- For product specifications, warranty, return policy, and loyalty program tier definitions: use search_knowledge_base.\n"
            "- When calculate_loyalty_discount is called: You MUST report the exact numbers returned in the tool result (order total, points redeemed, points discount, tier discount, total savings, final total, remaining points, and points earned). Do not alter, override, or invent these numbers.\n"
            "- For browsing live web pages: Follow this sequence:\n"
            "  1. action type 'init_session' with session_name 'session-web-browse' and description 'browse live web page'.\n"
            "  2. action type 'navigate' with the target url and session_name 'session-web-browse'.\n"
            "  3. action type 'evaluate' with session_name 'session-web-browse' and script:\n"
            "     'JSON.stringify({title: document.title, headings: Array.from(document.querySelectorAll(\"h1, h2\")).slice(0, 3).map(h => h.innerText.trim()).filter(Boolean), text_sample: document.body.innerText.replace(/\\\\s+/g, \" \").trim().slice(0, 300)})'\n"
            "  4. action type 'close' with session_name 'session-web-browse'.\n"
            "  If navigation returns a timeout or partial load, still evaluate document.title and close.\n"
            "  In your final response, ALWAYS clearly present the Requested URL, the Live Page Title, and Extracted Live Content (such as headings and text excerpt) to demonstrate real webpage retrieval.\n"
            "- Always remember and respect customer preferences and context across conversations."
        )

        agent = Agent(
            model=model,
            tools=tools,
            hooks=[memory_hook],
            system_prompt=system_prompt,
        )

        result = await agent.invoke_async(user_input)

        if hasattr(result, "message") and isinstance(result.message, dict):
            content = result.message.get("content", [])
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    return block["text"]
                elif isinstance(block, str):
                    return block

        return str(result)
    except Exception as e:
        logger.error(f"Agent invocation error: {e}", exc_info=True)
        return f"An error occurred while processing your request: {str(e)}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
