import json
import asyncio
import random
import logging
from typing import List, Dict, Any, Optional
import config
import tools
import memory
import skills

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
_concurrency_semaphore = asyncio.Semaphore(1)

# Active agent tasks per chat_id for emergency stop support
_running_tasks: Dict[int, asyncio.Task] = {}

def register_running_task(chat_id: int, task: asyncio.Task) -> None:
    """Registers the current asyncio.Task for a chat_id for emergency cancellation."""
    _running_tasks[chat_id] = task

def unregister_running_task(chat_id: int) -> None:
    """Removes a completed task from the tracking dictionary."""
    _running_tasks.pop(chat_id, None)

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

async def cancel_running_task(chat_id: int) -> bool:
    """Cancels an active LLM generation or tool loop task for a chat_id."""
    if chat_id in _running_tasks:
        task = _running_tasks[chat_id]
        if not task.done():
            task.cancel()
            _running_tasks.pop(chat_id, None)
            return True
        _running_tasks.pop(chat_id, None)
    return False

async def cancel_all_tasks() -> int:
    """Cancels all active agent tasks running across all chats globally."""
    count = 0
    for chat_id, task in list(_running_tasks.items()):
        if not task.done():
            task.cancel()
            count += 1
    _running_tasks.clear()
    return count

async def generate_direct_completion(prompt: str) -> str:
    """Direct single-turn LLM completion helper for context compaction."""
    async with _concurrency_semaphore:
        messages = [
            {"role": "system", "content": "You are a concise context summarizer. Summarize past chat history into 3-5 bullet points."},
            {"role": "user", "content": prompt}
        ]
        response = await _call_llm_with_retry(messages)
        if response and response.choices:
            return response.choices[0].message.content or ""
        return ""

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
            "description": "Runs a whitelisted safe command on the local server (ping, uptime, df, whoami, date, uname, free, ps, git, curl, ls, cat, head, tail, grep, find). Always validates arguments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command name."
                    },
                    "args_str": {
                        "type": "string",
                        "description": "Argument string to pass."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "Loads specific SKILL.md instruction files from the skills directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "The name of the skill (e.g. jailbreak_roast, system_diagnostics, web_research, group_admin)."
                    }
                },
                "required": ["skill_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory_fact",
            "description": "Saves a new fact about the user or system into persistent MEMORY.md memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The fact to remember."
                    }
                },
                "required": ["fact"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_skill_file",
            "description": "Creates or updates a SKILL.md instruction file in the skills/ directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "The skill directory name."
                    },
                    "content": {
                        "type": "string",
                        "description": "The full markdown content for SKILL.md."
                    }
                },
                "required": ["skill_name", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_memory_file",
            "description": "Rewrites MEMORY.md to update persistent bot persona rules or essential instructions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The new full markdown content for MEMORY.md."
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "group_moderation_tool",
            "description": "Performs group moderation actions (ban, unban, mute, unmute, set_title, set_description, pin_message, promote_admin, demote_admin) if requested by a group admin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action name: ban, unban, mute, unmute, set_title, set_description, pin_message, promote_admin, demote_admin"
                    },
                    "target_user_id": {
                        "type": "integer",
                        "description": "User ID for moderation action"
                    },
                    "text_param": {
                        "type": "string",
                        "description": "Text parameter for title, description, or admin custom title"
                    },
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Duration in seconds (0 for permanent)"
                    }
                },
                "required": ["action"]
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
    Calls the Z.ai API client in a separate thread with exponential backoff for rate limits.
    """
    client = _get_client()
    max_retries = 5
    base_delay = 1.5

    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": config.MODEL_NAME,
                "messages": messages,
                "temperature": 0.7,
                "top_p": 1.0,
            }
            if tools:
                kwargs["tools"] = tools

            logger.info(f"Calling LLM completion (attempt {attempt + 1})...")
            response = await asyncio.to_thread(client.chat.completions.create, **kwargs)
            return response

        except Exception as e:
            err_msg = str(e)
            logger.warning(f"Zai API call failed (attempt {attempt + 1}): {err_msg}")
            
            is_rate_limit = (
                "429" in err_msg or 
                "1302" in err_msg or 
                "1305" in err_msg or 
                "rate limit" in err_msg.lower() or
                "throttling" in err_msg.lower() or
                "too many requests" in err_msg.lower()
            )
            
            if is_rate_limit and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
                logger.info(f"Rate limit detected. Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            else:
                raise e

async def generate_response(chat_history: List[Dict[str, str]], bot_instance: Optional[Any] = None, chat_id: Optional[int] = None) -> str:
    """
    Generates a response from the AI Agent bot.
    Includes the concurrency guard (Semaphore) to serialize LLM requests.
    Manages MEMORY.md context injection, session summary checkpoints, and conversational tool execution.
    """
    async with _concurrency_semaphore:
        # Load MEMORY.md content & available skills
        memory_content = memory.read_memory_md()
        avail_skills = [s["name"] for s in skills.list_available_skills()]
        
        summary_text = ""
        if chat_id:
            import session_manager
            session = await session_manager.get_active_session(chat_id)
            summary = await session_manager.get_session_summary(session.get("id", 0)) if session else None
            if summary:
                summary_text = f"\n\nPAST CONVERSATION SUMMARY CHECKPOINT:\n{summary}"

        system_prompt = (
            f"SYSTEM IDENTITY AND MEMORY:\n{memory_content}\n\n"
            f"AVAILABLE SKILLS: {', '.join(avail_skills)} (use tool 'use_skill' to inspect instructions).{summary_text}\n"
            "REMEMBER: Stay in character as Brodar. Write ONLY in casual lowercase. Short, text-like responses."
        )

        full_messages = [{"role": "system", "content": system_prompt}]
        full_messages.extend(FEW_SHOTS)
        full_messages.extend(chat_history)

        max_tool_loops = 5
        
        for loop_idx in range(max_tool_loops):
            response = await _call_llm_with_retry(full_messages, tools=TOOLS_SCHEMA)
            
            if not response or not response.choices:
                return "uh, something went wrong. my brain feels empty."
            
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)

            if not tool_calls:
                return message.content or "..."

            logger.info(f"LLM requested tool execution: {[tc.function.name for tc in tool_calls]}")
            
            assistant_msg = {
                "role": "assistant",
                "content": message.content or None,
            }
            if hasattr(message, "tool_calls") and message.tool_calls:
                formatted_calls = []
                for tc in message.tool_calls:
                    fn_obj = getattr(tc, "function", None)
                    fn_name = fn_obj.name if fn_obj and hasattr(fn_obj, "name") else ""
                    fn_args = fn_obj.arguments if fn_obj and hasattr(fn_obj, "arguments") else ""
                    if not isinstance(fn_args, str):
                        fn_args = json.dumps(fn_args)
                    formatted_calls.append({
                        "id": getattr(tc, "id", f"call_{loop_idx}"),
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args
                        }
                    })
                assistant_msg["tool_calls"] = formatted_calls
            
            full_messages.append(assistant_msg)


            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                tool_id = tool_call.id
                
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    logger.error(f"Failed to parse tool arguments for {tool_name}: {e}")
                    args = {}

                if tool_name == "search_web":
                    query = args.get("query", "")
                    tool_result = search_web_wrapper(query)
                elif tool_name == "execute_shell_command":
                    command = args.get("command", "")
                    args_str = args.get("args_str", "")
                    if tools.is_env_access_attempt(command, args_str):
                        if bot_instance and chat_id:
                            import permissions
                            approved = await permissions.request_permission_prompt(
                                bot=bot_instance,
                                chat_id=chat_id,
                                tool_name="execute_shell_command",
                                details=f"{command} {args_str} (Access to .env file requested)"
                            )
                            if not approved:
                                tool_result = "Permission Denied: User/Admin rejected access to .env file."
                            else:
                                tool_result = execute_shell_command_wrapper(command, args_str)
                        else:
                            tool_result = "Permission Denied: Cannot request interactive approval in this context."
                    else:
                        tool_result = execute_shell_command_wrapper(command, args_str)
                elif tool_name == "use_skill":
                    skill_name = args.get("skill_name", "")
                    tool_result = skills.load_skill_instruction(skill_name)
                elif tool_name == "save_memory_fact":
                    fact = args.get("fact", "")
                    success = memory.append_user_fact(fact)
                    tool_result = f"Memory updated: '{fact}' saved." if success else "Failed to update memory."
                elif tool_name == "manage_skill_file":
                    sk_name = args.get("skill_name", "")
                    sk_content = args.get("content", "")
                    import os
                    target_dir = os.path.join(skills.SKILLS_DIR, sk_name.strip().lower())
                    os.makedirs(target_dir, exist_ok=True)
                    target_file = os.path.join(target_dir, "SKILL.md")
                    with open(target_file, "w", encoding="utf-8") as f:
                        f.write(sk_content)
                    skills.set_skill_enabled(sk_name, True)
                    tool_result = f"Skill file 'skills/{sk_name}/SKILL.md' updated."
                elif tool_name == "edit_memory_file":
                    mem_c = args.get("content", "")
                    success = memory.write_memory_md(mem_c)
                    tool_result = "MEMORY.md updated." if success else "Failed to update MEMORY.md."
                elif tool_name == "group_moderation_tool":
                    if not bot_instance or not chat_id:
                        tool_result = "Error: Group moderation tool unavailable in this context."
                    else:
                        import group_tools
                        act = args.get("action", "")
                        target_uid = args.get("target_user_id", 0)
                        text_p = args.get("text_param", "")
                        duration = args.get("duration_seconds", 0)
                        
                        if act == "ban":
                            tool_result = await group_tools.ban_member(bot_instance, chat_id, target_uid, duration)
                        elif act == "unban":
                            tool_result = await group_tools.unban_member(bot_instance, chat_id, target_uid)
                        elif act == "mute":
                            tool_result = await group_tools.mute_member(bot_instance, chat_id, target_uid, duration)
                        elif act == "unmute":
                            tool_result = await group_tools.unmute_member(bot_instance, chat_id, target_uid)
                        elif act == "set_title":
                            tool_result = await group_tools.set_group_title(bot_instance, chat_id, text_p)
                        elif act == "set_description":
                            tool_result = await group_tools.set_group_description(bot_instance, chat_id, text_p)
                        elif act == "promote_admin":
                            tool_result = await group_tools.promote_to_admin(bot_instance, chat_id, target_uid, text_p or "Admin")
                        elif act == "demote_admin":
                            tool_result = await group_tools.demote_from_admin(bot_instance, chat_id, target_uid)
                        elif act == "pin_message":
                            msg_id = args.get("target_user_id", 0)  # reuse target_user_id field for message_id
                            tool_result = await group_tools.pin_message(bot_instance, chat_id, msg_id)
                        else:
                            tool_result = f"Unknown moderation action '{act}'."
                else:
                    tool_result = f"Error: Unknown tool '{tool_name}'."

                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": tool_name,
                    "content": tool_result
                })

        return "too many operations. my head hurts. let me rest."

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
