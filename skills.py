import os
import glob
import json
import shutil
import urllib.request
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
STATE_FILE = os.path.join(SKILLS_DIR, "skills_state.json")

def _load_state() -> Dict[str, bool]:
    """Loads skill enabled/disabled state."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading skills_state.json: {e}")
    return {}

def _save_state(state: Dict[str, bool]) -> None:
    """Saves skill enabled/disabled state."""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving skills_state.json: {e}")

def is_skill_enabled(skill_name: str) -> bool:
    """Checks if a skill is enabled (defaults to True)."""
    state = _load_state()
    return state.get(skill_name.strip().lower(), True)

def set_skill_enabled(skill_name: str, enabled: bool) -> bool:
    """Enables or disables a skill."""
    skill_name = skill_name.strip().lower()
    state = _load_state()
    state[skill_name] = enabled
    _save_state(state)
    return True

def list_available_skills(enabled_only: bool = True) -> List[Dict[str, Any]]:
    """
    Scans skills/ for SKILL.md files.
    Returns list of skill info dicts.
    """
    skills = []
    if not os.path.exists(SKILLS_DIR):
        return skills

    for skill_path in glob.glob(f"{SKILLS_DIR}/**/SKILL.md", recursive=True):
        skill_name = os.path.basename(os.path.dirname(skill_path))
        enabled = is_skill_enabled(skill_name)
        
        if enabled_only and not enabled:
            continue

        description = "No description provided."
        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        for line in parts[1].splitlines():
                            if line.startswith("description:"):
                                description = line.split("description:", 1)[1].strip()
        except Exception as e:
            logger.error(f"Error reading skill {skill_name}: {e}")

        skills.append({
            "name": skill_name,
            "path": skill_path,
            "description": description,
            "enabled": enabled
        })
    return skills

def load_skill_instruction(skill_name: str) -> str:
    """Reads and returns the full instruction content of a specific SKILL.md."""
    skill_name = skill_name.strip().lower()
    if not is_skill_enabled(skill_name):
        return f"Skill '{skill_name}' is currently disabled."

    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.exists(skill_path):
        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading skill {skill_name}: {e}")
            return f"Error loading skill '{skill_name}': {e}"
    
    return f"Skill '{skill_name}' not found."

def install_skill_from_url(url: str, custom_name: Optional[str] = None) -> str:
    """
    Downloads SKILL.md content from a raw URL and installs it into skills/<name>/SKILL.md.
    """
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return "Error: URL must start with http:// or https://"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BrodarBot/1.0"})
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            content = resp.read().decode("utf-8")

        # Determine skill name from parameter or URL
        skill_name = custom_name
        if not skill_name:
            parts = [p for p in url.split("/") if p and p != "SKILL.md" and not p.endswith(".git")]
            skill_name = parts[-1] if parts else "external_skill"

        skill_name = "".join(c for c in skill_name.lower() if c.isalnum() or c in "_-")
        target_dir = os.path.join(SKILLS_DIR, skill_name)
        os.makedirs(target_dir, exist_ok=True)

        target_file = os.path.join(target_dir, "SKILL.md")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(content)

        set_skill_enabled(skill_name, True)
        return f"Skill '{skill_name}' installed successfully to skills/{skill_name}/SKILL.md."
    except Exception as e:
        logger.error(f"Failed to install skill from {url}: {e}")
        return f"Failed to install skill from URL: {e}"

def uninstall_skill(skill_name: str) -> str:
    """Removes a skill directory."""
    skill_name = skill_name.strip().lower()
    target_dir = os.path.join(SKILLS_DIR, skill_name)
    if os.path.exists(target_dir):
        try:
            shutil.rmtree(target_dir)
            state = _load_state()
            state.pop(skill_name, None)
            _save_state(state)
            return f"Skill '{skill_name}' uninstalled and removed."
        except Exception as e:
            return f"Failed to uninstall skill '{skill_name}': {e}"
    return f"Skill '{skill_name}' not found."

