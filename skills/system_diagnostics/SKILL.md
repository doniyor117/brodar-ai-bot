---
name: system_diagnostics
description: Instructions for inspecting system uptime, memory usage, disk space, and process status.
---
# System Diagnostics Skill

When asked about server status, health, RAM, or processes:
1. Use `execute_shell_command` with whitelisted binaries (`uptime`, `free`, `df`, `ps`, `whoami`, `date`, `uname`).
2. Summarize the command output clearly and in Brodar's casual lowercase persona.
3. Highlight any anomalies (e.g. high CPU or low disk space) if detected.
