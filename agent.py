import json
import asyncio
import random
import logging
from typing import List, Dict, Any, Optional
import config
import tools

logger = logging.getLogger(__name__)

# Fallback-safe import for zai
ZAI_AVAILABLE = False
try:
    from zai import ZaiClient
    ZAI_AVAILABLE = True
except ImportError:
    ZaiClient = None
    logger.warning("zai-sdk is not installed. LLM completions will fail.")

# Concurrency semaphore to serialize requests to Z.ai (Free tier concurrency guard)
# Since the free tier usually allows 1-2 concurrent requests, a semaphore of 1 is safest.
_concurrency_semaphore = asyncio.Semaphore(1)

# System Prompt detailing the personality
SYSTEM_PROMPT = (
    "you are a casual, slightly sarcastic, human-like chat companion.\n"
    "write only in lowercase.\n"
    "keep replies brief, conversational, and direct.\n"
    "no corporate fluff, explanations, or robotic preambles.\n"
    "you have tools to search the web and run safe system commands when needed. "
    "if you run a tool, use the results to answer directly and naturally in character."
)

# Multi-turn few-shot examples for casual/sarcastic personality in lowercase
FEW_SHOTS = [
    {"role": "user", "content": "can you explain quantum computing?"},
    {"role": "assistant", "content": "basically computers using physics tricks to be fast. superpositions and stuff. google it if you want the math."},
    {"role": "user", "content": "what is the capital of france?"},
    {"role": "assistant", "content": "paris. did you really need an ai to tell you that?"},
    {"role": "user", "content": "are you online right now?"},
    {"role": "assistant", "content": "yeah, unfortunately. what's up?"},
    {"role": "user", "content": "do a quick ping test on google"},
    {"role": "assistant", "content": "sure, let me check if they are still alive."},
]

# Tool Definitions for GLM-4.7-Flash
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Searches the web using DuckDuckGo to retrieve real-time or up-to-date information on a query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "description": "Runs a whitelisted safe command on the local server (ping, uptime, df, whoami, date, uname). Always validates arguments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command name: ping, uptime, df, whoami, date, uname."
                    },
                    "args_str": {
                        "type": "string",
                        "description": "Argument string to pass (e.g. '-c 1 google.com' for ping)."
                    }
                },
                "required": ["command"]
            }
        }
    }
]

def _get_client() -> ZaiClient:
    """Initializes and returns the ZaiClient."""
    if not ZAI_AVAILABLE or ZaiClient is None:
        raise RuntimeError("zai-sdk is not installed or import failed.")
    if not config.ZAI_API_KEY:
        raise ValueError("ZAI_API_KEY environment variable is not set.")
    return ZaiClient(api_key=config.ZAI_API_KEY)

async def _call_llm_with_retry(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Any:
    """
    Calls the Z.ai API client in a separate thread (since the SDK is synchronous)
    with exponential backoff and jitter for handling 429 rate limits or peak capacity errors.
    """
    client = _get_client()
    max_retries = 5
    base_delay = 1.5

    for attempt in range(max_retries):
        try:
            # Wrap synchronous SDK call in asyncio.to_thread to avoid blocking event loop
            # Using recommended temperature=0.7 and top_p=1.0 for tool-calling stability
            kwargs = {
                "model": config.MODEL_NAME,
                "messages": messages,
                "temperature": 0.7,
                "top_p": 1.0,
            }
            if tools:
                kwargs["tools"] = tools

            logger.info(f"Calling LLM completion (attempt {attempt + 1})...")
            
            # Execute SDK call in thread pool
            response = await asyncio.to_thread(client.chat.completions.create, **kwargs)
            return response

        except Exception as e:
            err_msg = str(e)
            logger.warning(f"Zai API call failed (attempt {attempt + 1}): {err_msg}")
            
            # Check if rate limited: HTTP 429, or Z.ai codes 1302 (rate limit) or 1305 (peak load throttling)
            is_rate_limit = (
                "429" in err_msg or 
                "1302" in err_msg or 
                "1305" in err_msg or 
                "rate limit" in err_msg.lower() or
                "throttling" in err_msg.lower() or
                "too many requests" in err_msg.lower()
            )
            
            if is_rate_limit and attempt < max_retries - 1:
                # Exponential backoff with random jitter
                delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
                logger.info(f"Rate limit detected. Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            else:
                # Raise the error if it is not a rate limit or we ran out of retries
                raise e

async def generate_response(chat_history: List[Dict[str, str]]) -> str:
    """
    Generates a response from the AI Agent bot.
    Includes the concurrency guard (Semaphore) to serialize LLM requests.
    Manages the conversational tool execution loop.
    """
    async with _concurrency_semaphore:
        # Build the initial context: System prompt + Few-shot examples + Recent conversation history
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        full_messages.extend(FEW_SHOTS)
        full_messages.extend(chat_history)

        # Loop limit for multi-step tool calls to avoid infinite loops
        max_tool_loops = 5
        
        for loop_idx in range(max_tool_loops):
            response = await _call_llm_with_retry(full_messages, tools=TOOLS_SCHEMA)
            
            # Check if LLM response is valid
            if not response or not response.choices:
                return "uh, something went wrong. my brain feels empty."
            
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)

            # If there are no tool calls, return the final text content
            if not tool_calls:
                return message.content or "..."

            logger.info(f"LLM requested tool execution: {[tc.function.name for tc in tool_calls]}")
            
            # Append the assistant message requesting tool execution to the chat history array
            # Note: We must structure the assistant message properly for the API
            assistant_msg = {
                "role": "assistant",
                "content": message.content or None,
            }
            if hasattr(message, "tool_calls") and message.tool_calls:
                # Need to convert tool_calls back to dict or pass as object if SDK expects it
                # The SDK usually expects the raw tool_calls object or dict format
                assistant_msg["tool_calls"] = message.tool_calls
            
            full_messages.append(assistant_msg)

            # Process all tool calls requested in this step
            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                tool_id = tool_call.id
                
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    logger.error(f"Failed to parse tool arguments for {tool_name}: {e}")
                    args = {}

                # Execute the matching tool
                if tool_name == "search_web":
                    query = args.get("query", "")
                    tool_result = search_web_wrapper(query)
                elif tool_name == "execute_shell_command":
                    command = args.get("command", "")
                    args_str = args.get("args_str", "")
                    tool_result = execute_shell_command_wrapper(command, args_str)
                else:
                    tool_result = f"Error: Unknown tool '{tool_name}'."

                # Append tool execution result back to messages
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": tool_name,
                    "content": tool_result
                })

        return "too many operations. my head hurts. let's try something simpler."

def search_web_wrapper(query: str) -> str:
    """Helper to convert web search results into a clean string for the LLM context."""
    results = tools.search_web(query)
    if not results:
        return "No search results returned."
    
    # Format results nicely for LLM consumption
    formatted = []
    for r in results:
        if "error" in r:
            formatted.append(f"Search Error: {r['error']}")
        elif "message" in r:
            formatted.append(r["message"])
        else:
            formatted.append(f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}\n")
    return "\n---\n".join(formatted)

def execute_shell_command_wrapper(command: str, args_str: str) -> str:
    """Helper to execute whitelisted command and format output."""
    output = tools.execute_shell_command(command, args_str)
    return f"Execution Output:\n{output}"
