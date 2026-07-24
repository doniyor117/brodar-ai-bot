import os
import re
import shlex
import shutil
import subprocess
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# Fallback-safe import for ddgs
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None
        logger.warning("Neither 'ddgs' nor 'duckduckgo_search' package is available.")

# Strict whitelist of allowed commands, their executable names, and allowed argument regex patterns.
# Only arguments matching the regex will be allowed to execute.
ALLOWED_COMMANDS = {
    "ping": {
        "bin": "ping",
        "args_regex": r"^-[c]\s+[1-3]\s+[a-zA-Z0-9.-]+$", # Only allow e.g., -c 1 google.com
        "description": "Pings a host. Arguments must match: -c [1-3] [host]"
    },
    "uptime": {
        "bin": "uptime",
        "args_regex": r"^$", # No arguments allowed
        "description": "Shows system uptime. No arguments allowed."
    },
    "df": {
        "bin": "df",
        "args_regex": r"^$|^-[hT]$", # No arguments or simple flags
        "description": "Shows disk space usage. Arguments allowed: none, -h, -T"
    },
    "whoami": {
        "bin": "whoami",
        "args_regex": r"^$", # No arguments allowed
        "description": "Shows current user. No arguments allowed."
    },
    "date": {
        "bin": "date",
        "args_regex": r"^$|^-[u]$", # No arguments or -u for UTC
        "description": "Shows current date and time. Arguments allowed: none, -u"
    },
    "uname": {
        "bin": "uname",
        "args_regex": r"^$|^-[arsn]$", # No arguments or simple flags
        "description": "Shows system information. Arguments allowed: none, -a, -r, -s, -n"
    }
}

def search_web(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Executes a web search on DuckDuckGo.
    Returns a list of dicts with title, url, and snippet.
    """
    if DDGS is None:
        return [{"error": "DuckDuckGo search package is not installed/available."}]

    if not query.strip():
        return [{"error": "Empty search query."}]

    logger.info(f"Executing web search for: '{query}'")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            if not results:
                return [{"message": "No results found."}]
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")
                }
                for r in results
            ]
    except Exception as e:
        logger.error(f"DuckDuckGo search error: {e}", exc_info=True)
        return [{"error": f"Search failed: {str(e)}"}]

def execute_shell_command(command: str, args_str: str = "") -> str:
    """
    Executes a whitelisted shell command safely using subprocess without shell=True.
    Validates arguments against a strict regex whitelist.
    Strips sensitive environment variables.
    """
    command = command.strip().lower()
    args_str = args_str.strip()

    # 1. Check if command is whitelisted
    if command not in ALLOWED_COMMANDS:
        return f"Error: Command '{command}' is not in the whitelist. Allowed: {', '.join(ALLOWED_COMMANDS.keys())}"

    cmd_config = ALLOWED_COMMANDS[command]
    regex = cmd_config["args_regex"]

    # 2. Validate arguments against regex
    if not re.match(regex, args_str):
        return f"Error: Arguments '{args_str}' do not match the validation pattern for '{command}'."

    # 3. Resolve path to binary
    bin_name = cmd_config["bin"]
    bin_path = shutil.which(bin_name)
    if not bin_path:
        return f"Error: Executable '{bin_name}' not found on the system."

    # 4. Parse arguments safely
    # shlex.split parses arguments as a list, preserving quoted arguments
    try:
        args_list = shlex.split(args_str)
    except Exception as e:
        return f"Error: Failed to parse arguments: {str(e)}"

    # 5. Sanitize environment (remove secrets)
    env = os.environ.copy()
    secrets_to_strip = [
        "TELEGRAM_BOT_TOKEN",
        "ZAI_API_KEY",
        "DATABASE_URL",
        "WEBHOOK_SECRET_TOKEN"
    ]
    for secret in secrets_to_strip:
        env.pop(secret, None)

    # 6. Execute the command with a strict timeout
    full_cmd = [bin_path] + args_list
    logger.info(f"Executing safe command: {full_cmd}")

    try:
        result = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=5.0, # Strict 5s timeout
            env=env
        )
        output = result.stdout
        if result.stderr:
            output += f"\nError Output:\n{result.stderr}"
        if not output.strip():
            return "[Command completed with no output]"
        return output
    except subprocess.TimeoutExpired:
        logger.warning(f"Command execution timed out: {full_cmd}")
        return "Error: Command execution timed out after 5.0 seconds."
    except Exception as e:
        logger.error(f"Error running command {full_cmd}: {e}", exc_info=True)
        return f"Error: Internal execution failure: {str(e)}"
