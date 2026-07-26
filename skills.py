"""
Skill loading: Hermes-style SKILL.md instruction files under skills/<slug>/.

Every skill has ONE canonical identity — its directory name, the `slug`. The
frontmatter `name` is a display label only. They used to be used
interchangeably: list/advertise used the frontmatter name, enable/disable/
uninstall used the directory name, and load matched on the frontmatter name.
So `bot-architecture` (frontmatter name "Brodar Bot Architecture") could be
advertised under one name, was loadable only under the other, and disabling it
by the name the user could actually see reported "not found".

The index is cached behind an mtime check. It was previously re-globbed,
re-opened and re-parsed on every single request, with skills_state.json re-read
once per skill, all synchronously on the event loop.
"""
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


def slugify(value: str) -> str:
    """Normalize any user/model-supplied skill reference to a comparable form."""
    return "".join(
        c for c in (value or "").strip().lower().replace(" ", "-")
        if c.isalnum() or c in "_-"
    )


# ── state ───────────────────────────────────────────────────────────────────
_state_cache: Optional[Dict[str, bool]] = None
_state_mtime: float = -1.0


def _load_state() -> Dict[str, bool]:
    """Skill enabled/disabled map, re-read only when the file actually changes."""
    global _state_cache, _state_mtime

    try:
        mtime = os.path.getmtime(STATE_FILE)
    except OSError:
        _state_cache, _state_mtime = {}, -1.0
        return {}

    if _state_cache is not None and mtime == _state_mtime:
        return _state_cache

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        _state_cache = {slugify(k): bool(v) for k, v in loaded.items()}
        _state_mtime = mtime
    except Exception as e:
        logger.error(f"Error loading skills_state.json: {e}")
        _state_cache = {}
    return _state_cache


def _save_state(state: Dict[str, bool]) -> None:
    """Persist the enabled/disabled map and invalidate the caches."""
    global _state_cache, _state_mtime
    os.makedirs(SKILLS_DIR, exist_ok=True)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        _state_cache = dict(state)
        _state_mtime = os.path.getmtime(STATE_FILE)
    except Exception as e:
        logger.error(f"Error saving skills_state.json: {e}")
    _invalidate_index()


def is_skill_enabled(skill_name: str) -> bool:
    """Whether a skill is enabled (defaults to True). Accepts a slug or a name."""
    return _load_state().get(_resolve_slug(skill_name) or slugify(skill_name), True)


def set_skill_enabled(skill_name: str, enabled: bool) -> bool:
    """Enable or disable a skill by slug or display name."""
    slug = _resolve_slug(skill_name) or slugify(skill_name)
    if not slug:
        return False
    state = dict(_load_state())
    state[slug] = enabled
    _save_state(state)
    return True


# ── parsing ─────────────────────────────────────────────────────────────────
def _parse_frontmatter(content: str) -> tuple:
    """
    Parse YAML-ish frontmatter from a SKILL.md.
    Returns (frontmatter_dict, markdown_body).
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            frontmatter[key.strip()] = val.strip()

    return frontmatter, parts[2].strip()


def preprocess_skill_content(content: str, skill_dir: str) -> str:
    """Apply template variables to skill content. Supported: ${SKILL_DIR}"""
    if not content:
        return content
    return content.replace("${SKILL_DIR}", skill_dir)


# ── index ───────────────────────────────────────────────────────────────────
_index_cache: Optional[List[Dict[str, Any]]] = None
_index_signature: Optional[tuple] = None


def _invalidate_index() -> None:
    global _index_cache, _index_signature
    _index_cache, _index_signature = None, None


def _dir_signature() -> tuple:
    """
    Cheap fingerprint of the skills tree: every SKILL.md path and its mtime.

    Only stats files — no reads, no parsing — so the common case (nothing
    changed) costs a handful of stat() calls instead of opening and parsing
    every skill on every request.
    """
    paths = sorted(glob.glob(f"{SKILLS_DIR}/**/SKILL.md", recursive=True))
    sig = []
    for p in paths:
        try:
            sig.append((p, os.path.getmtime(p)))
        except OSError:
            continue
    return tuple(sig)


def _build_index() -> List[Dict[str, Any]]:
    """Read and parse every SKILL.md. Only called when the tree has changed."""
    out = []
    if not os.path.exists(SKILLS_DIR):
        return out

    for skill_path in sorted(glob.glob(f"{SKILLS_DIR}/**/SKILL.md", recursive=True)):
        skill_dir = os.path.dirname(skill_path)
        slug = slugify(os.path.basename(skill_dir))

        display_name, description, body = slug, "No description provided.", ""
        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
            frontmatter, raw_body = _parse_frontmatter(content)
            description = frontmatter.get("description", description)
            display_name = frontmatter.get("name", slug)
            body = preprocess_skill_content(raw_body, skill_dir)
        except Exception as e:
            logger.error(f"Error reading skill '{slug}': {e}")

        out.append({
            "slug": slug,
            "name": display_name,
            "path": skill_path,
            "description": description,
            "body": body,
        })
    return out


def _get_index() -> List[Dict[str, Any]]:
    """The parsed skill index, rebuilt only when a SKILL.md changes."""
    global _index_cache, _index_signature

    signature = _dir_signature()
    if _index_cache is not None and signature == _index_signature:
        return _index_cache

    _index_cache = _build_index()
    _index_signature = signature
    return _index_cache


def _resolve_slug(reference: str) -> Optional[str]:
    """
    Map anything the model might call a skill to its canonical slug.

    Matches the slug, the display name, or a slugified display name, so
    "bot-architecture", "Brodar Bot Architecture" and "brodar bot architecture"
    all reach the same skill.
    """
    wanted = slugify(reference)
    if not wanted:
        return None
    for entry in _get_index():
        if wanted in (entry["slug"], slugify(entry["name"])):
            return entry["slug"]
    return None


def list_available_skills(enabled_only: bool = True) -> List[Dict[str, Any]]:
    """
    Every installed skill. Each dict carries both `slug` (canonical) and `name`
    (display), plus `description` and `enabled`.
    """
    state = _load_state()
    out = []
    for entry in _get_index():
        enabled = state.get(entry["slug"], True)
        if enabled_only and not enabled:
            continue
        item = dict(entry)
        item["enabled"] = enabled
        item.pop("body", None)
        out.append(item)
    return out


def get_skill_body(skill_name: str) -> Optional[str]:
    """The instruction body of an enabled skill, or None if unavailable."""
    slug = _resolve_slug(skill_name)
    if not slug or not is_skill_enabled(slug):
        return None
    for entry in _get_index():
        if entry["slug"] == slug:
            return entry["body"]
    return None


def load_skill_instruction(skill_name: str) -> str:
    """Full instruction text of a skill, for the use_skill tool."""
    slug = _resolve_slug(skill_name)
    if not slug:
        known = ", ".join(s["slug"] for s in list_available_skills()) or "(none)"
        return f"Skill '{skill_name}' not found. Available skills: {known}."

    if not is_skill_enabled(slug):
        return f"Skill '{slug}' is currently disabled."

    body = get_skill_body(slug)
    if not body:
        return f"Skill '{slug}' has no readable instructions."
    return body


def install_skill_from_url(url: str, custom_name: Optional[str] = None) -> str:
    """Download a SKILL.md from a raw URL into skills/<slug>/SKILL.md."""
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return "Error: URL must start with http:// or https://"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BrodarBot/1.0"})
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            content = resp.read().decode("utf-8")

        skill_name = custom_name
        if not skill_name:
            parts = [p for p in url.split("/") if p and p != "SKILL.md" and not p.endswith(".git")]
            skill_name = parts[-1] if parts else "external_skill"

        slug = slugify(skill_name)
        if not slug:
            return "Error: could not derive a valid skill name from that URL."

        target_dir = os.path.join(SKILLS_DIR, slug)
        # Defense in depth: the slug is already sanitized, but never let a name
        # escape the skills directory.
        if os.path.realpath(target_dir) != os.path.join(os.path.realpath(SKILLS_DIR), slug):
            return "Error: invalid skill path."

        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(content)

        _invalidate_index()
        set_skill_enabled(slug, True)
        return f"Skill '{slug}' installed successfully to skills/{slug}/SKILL.md."
    except Exception as e:
        logger.error(f"Failed to install skill from {url}: {e}")
        return f"Failed to install skill from URL: {e}"


def uninstall_skill(skill_name: str) -> str:
    """Delete a skill directory, by slug or display name."""
    slug = _resolve_slug(skill_name) or slugify(skill_name)
    if not slug:
        return f"Skill '{skill_name}' not found."

    target_dir = os.path.join(SKILLS_DIR, slug)
    if not os.path.isdir(target_dir):
        return f"Skill '{slug}' not found."

    try:
        shutil.rmtree(target_dir)
        state = dict(_load_state())
        state.pop(slug, None)
        _save_state(state)
        return f"Skill '{slug}' uninstalled and removed."
    except Exception as e:
        return f"Failed to uninstall skill '{slug}': {e}"
