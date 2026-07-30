---
name: web_research
description: Instructions for fetching real-time information using DuckDuckGo search.
---
# Web Research Skill

When a user asks for current news, facts, documentation, or real-time data:
1. Use the `search_web` tool with a concise search query.
2. If a result's snippet isn't enough — the user wants real detail, or the
   snippet is too thin to answer confidently — use `fetch_url` on the most
   relevant result's URL to read the actual page, rather than guessing from
   the snippet alone. See the `web-fetch-and-deliver` skill for what
   `fetch_url` can and can't do.
3. Synthesize the findings directly in character without copy-pasting raw search dumps.
4. Keep the tone casual and brief.
