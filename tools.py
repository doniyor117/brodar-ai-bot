import os
import re
import shlex
import shutil
import subprocess
import logging
from typing import List, Dict, Any

import config

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
        "args_regex": r"^-[c]\s+[1-3]\s+[a-zA-Z0-9.-]+$",
        "description": "Pings a host."
    },
    "uptime": {
        "bin": "uptime",
        "args_regex": r"^$",
        "description": "Shows system uptime."
    },
    "df": {
        "bin": "df",
        "args_regex": r"^$|^-[hT]$",
        "description": "Shows disk space usage."
    },
    "whoami": {
        "bin": "whoami",
        "args_regex": r"^$",
        "description": "Shows current user."
    },
    "date": {
        "bin": "date",
        "args_regex": r"^$|^-[u]$",
        "description": "Shows current date and time."
    },
    "uname": {
        "bin": "uname",
        "args_regex": r"^$|^-[arsn]$",
        "description": "Shows system info."
    },
    "free": {
        "bin": "free",
        "args_regex": r"^$|^-[h]$",
        "description": "Shows RAM usage."
    },
    "ps": {
        "bin": "ps",
        "args_regex": r"^$|^aux$|^aux\s+--sort=-%cpu$",
        "description": "Shows process status."
    },
    "git": {
        "bin": "git",
        "args_regex": r"^(status|log(\s+-n\s+[1-5])?|branch|diff)$",
        "description": "Shows git status, diff, or log."
    },
    "curl": {
        "bin": "curl",
        "args_regex": r"^-[I]\s+https://[a-zA-Z0-9.-]+$",
        "description": "Fetches HTTP headers for safe domains."
    },
    "ls": {
        "bin": "ls",
        "args_regex": r"^$|^-[la1h]+(\s+[a-zA-Z0-9_./-]+)?$",
        "description": "Lists directory contents.",
        "has_path_args": True,
    },
    "cat": {
        "bin": "cat",
        "args_regex": r"^[a-zA-Z0-9_./-]+$",
        "description": "Displays file content.",
        "has_path_args": True,
    },
    "head": {
        "bin": "head",
        "args_regex": r"^$|^(-n\s+[0-9]+\s+)?[a-zA-Z0-9_./-]+$",
        "description": "Displays top lines of a file.",
        "has_path_args": True,
    },
    "tail": {
        "bin": "tail",
        "args_regex": r"^$|^(-n\s+[0-9]+\s+)?[a-zA-Z0-9_./-]+$",
        "description": "Displays end lines of a file.",
        "has_path_args": True,
    },
    "grep": {
        "bin": "grep",
        "args_regex": r"^-[irn]+\s+['\"][a-zA-Z0-9_.-]+['\"]\s+[a-zA-Z0-9_./-]+$|^[a-zA-Z0-9_.-]+\s+[a-zA-Z0-9_./-]+$",
        "description": "Searches pattern in file.",
        "has_path_args": True,
    },
    "find": {
        "bin": "find",
        "args_regex": r"^$|^.+(-name|-type).+$",
        "description": "Finds files.",
        "has_path_args": True,
    },
    "cd": {
        "bin": "cd",
        "args_regex": r"^[a-zA-Z0-9_./-]+$|^\.\.$",
        "description": "Changes the current directory.",
        "has_path_args": True,
    }
}


def _workspace_dir() -> str:
    """Returns (and lazily creates) the sandbox directory shell tools run inside."""
    d = config.TOOL_WORKSPACE_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create tool workspace dir {d}: {e}")
    return d


def is_env_access_attempt(command: str, args_str: str) -> bool:
    """
    Kept for backwards compatibility. The real protection is the workspace
    sandbox in execute_shell_command; this is now just a fast obvious-case check.
    """
    text = f"{command} {args_str}".lower()
    return ".env" in text or "env." in text or "/env" in text


def _path_args_are_safe(args_list: List[str], workspace: str) -> bool:
    """
    Ensures every filesystem-path argument resolves to a location *inside* the
    workspace sandbox. This is what stops `grep -ri API .`, `cat ../.env`,
    absolute paths, and symlink tricks from ever reaching the secrets on disk.
    """
    real_workspace = os.path.realpath(workspace)
    for arg in args_list:
        # Skip flags and grep/find operators — only inspect things that look like paths.
        if arg.startswith("-") or arg == "-name":
            continue
        # A bare grep pattern (no slash, no dot-path) isn't a path; leave it.
        candidate = arg
        # Resolve relative to the workspace, following symlinks.
        resolved = os.path.realpath(os.path.join(real_workspace, candidate))
        if resolved != real_workspace and not resolved.startswith(real_workspace + os.sep):
            logger.warning(f"Blocked shell tool path escaping workspace: {arg!r} -> {resolved}")
            return False
    return True


def search_web(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Executes a web search. Uses Exa if EXA_API_KEY is present, else falls back to DuckDuckGo.
    Returns a list of dicts with title, url, and snippet.
    """
    if not query.strip():
        return [{"error": "Empty search query."}]

    logger.info(f"Executing web search for: '{query}'")
    
    exa_key = os.getenv("EXA_API_KEY")
    if exa_key:
        try:
            from exa_py import Exa
            exa = Exa(exa_key)
            res = exa.search_and_contents(query, num_results=max_results)
            if not res.results:
                return [{"message": "No results found."}]
            return [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.text[:500] if r.text else ""
                }
                for r in res.results
            ]
        except ImportError:
            logger.warning("EXA_API_KEY is set but exa_py package is missing. Falling back to DuckDuckGo.")
        except Exception as e:
            logger.error(f"Exa search error: {e}")
            return [{"error": f"Exa search failed: {str(e)}"}]

    if DDGS is None:
        return [{"error": "DuckDuckGo search package is not installed/available."}]

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

_terminal_sessions = {}

def execute_shell_command(command: str, args_str: str = "", chat_id: str = "default") -> str:
    """
    Executes a whitelisted shell command safely using subprocess without shell=True.
    Validates arguments against a strict regex whitelist, confines file-touching
    commands to a sandbox workspace, and strips sensitive environment variables.
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

    # 5. Confine filesystem commands to the sandbox workspace.
    # This is the core defense: .env and the source tree live OUTSIDE this dir,
    # so no combination of cat/grep/head/tail/find/ls can read secrets.
    base_workspace = _workspace_dir()
    
    current_cwd = _terminal_sessions.get(chat_id, base_workspace)
    if not os.path.exists(current_cwd):
        current_cwd = base_workspace
        _terminal_sessions[chat_id] = current_cwd
        
    if cmd_config.get("has_path_args"):
        # validate paths against base_workspace so they can't escape
        if not _path_args_are_safe(args_list, base_workspace):
            return "Error: Path arguments must stay inside the sandbox workspace. Access denied."

    # Handle cd built-in
    if command == "cd":
        if not args_list:
            return "Error: cd requires a path argument."
        target = args_list[0]
        new_cwd = os.path.realpath(os.path.join(current_cwd, target))
        if new_cwd != base_workspace and not new_cwd.startswith(base_workspace + os.sep):
            return "Error: Cannot cd outside of sandbox workspace."
        if not os.path.isdir(new_cwd):
            return f"Error: '{target}' is not a directory."
        _terminal_sessions[chat_id] = new_cwd
        return f"Changed directory to {new_cwd.replace(base_workspace, '~')}"

    # 6. Sanitize environment (remove secrets)
    env = os.environ.copy()
    secrets_to_strip = [
        "TELEGRAM_BOT_TOKEN",
        "ZAI_API_KEY",
        "DATABASE_URL",
        "WEBHOOK_SECRET_TOKEN",
    ]
    for secret in secrets_to_strip:
        env.pop(secret, None)

    # 7. Execute the command with a strict timeout, rooted in the sandbox.
    full_cmd = [bin_path] + args_list
    logger.info(f"Executing safe command in {current_cwd}: {full_cmd}")

    try:
        result = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=5.0,  # Strict 5s timeout
            env=env,
            cwd=current_cwd,
        )
        output = result.stdout
        if result.stderr:
            output += f"\nError Output:\n{result.stderr}"
        if not output.strip():
            return "[Command completed with no output]"
        # Cap output so a huge file can't blow past Telegram limits / context.
        return output[:3500]
    except subprocess.TimeoutExpired:
        logger.warning(f"Command execution timed out: {full_cmd}")
        return "Error: Command execution timed out after 5.0 seconds."
    except Exception as e:
        logger.error(f"Error running command {full_cmd}: {e}", exc_info=True)
        return f"Error: Internal execution failure: {str(e)}"
