"""
Phase 11 smoke checks — docs, commands, test harness.

    python3 test_phase11.py
"""
import os
import sys

import test_support

test_support.install_stubs()

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_dead_debug_stub_removed():
    check("the 9-line test_react.py debug stub is gone",
          not os.path.exists("test_react.py"))


def test_consolidated_runner_exists():
    check("run_tests.py exists", os.path.isfile("run_tests.py"))
    src = test_support.read_code("run_tests.py")
    check("it discovers every test_phase*.py automatically",
          "test_phase*.py" in src)


def test_readme_reflects_current_architecture():
    with open("README.md", encoding="utf-8") as f:
        content = f.read()
    check("no more stale zai-sdk claim", "zai-sdk" not in content)
    check("no more dead antigravity-cli paths", "antigravity-cli" not in content)
    check("no more claimed Semaphore(1) (concurrency is LLM_CONCURRENCY, default 2)",
          "Semaphore(1)" not in content)
    check("mentions LiteLLM as the actual engine", "litellm" in content.lower())
    check("documents the approval-to-DM flow", "approval_recipient_id" in content)
    check("documents the member-lookup Cyrillic/Latin folding", "Cyrillic" in content)
    check("documents the extraction/conversation task mode", "response_mode" in content)
    check("the real chat_members schema is described, not a stale 4-table one",
          "chat_members" in content and "recent_visuals" in content)


def test_env_example_documents_main_account_id():
    with open(".env.example", encoding="utf-8") as f:
        content = f.read()
    check("MAIN_ACCOUNT_ID is documented — it's load-bearing for approvals since Phase 9",
          "MAIN_ACCOUNT_ID" in content)


def test_setmain_command_name_is_correct_everywhere():
    # An actual bug caught during this pass: config.py and bot.py both told the
    # operator to run "/setmain", but the real registered command is
    # /set_main_account. The wrong hint would have sent someone typing a
    # command that doesn't exist.
    config_src = test_support.read_code("config.py")
    bot_src = test_support.read_code("bot.py")
    check("config.py's startup warning names the real command",
          "/set_main_account" in config_src)
    check("...and does not still say the wrong one",
          "/setmain" not in config_src)
    check("bot.py's /status output names the real command",
          "/set_main_account" in bot_src)
    check("...and does not still say the wrong one",
          "/setmain" not in bot_src)


def test_start_command_is_advertised_to_botfather():
    src = test_support.read_code("bot.py")
    start_idx = src.index("private_commands = [")
    end_idx = src.index("]", start_idx)
    window = src[start_idx:end_idx]
    check("/start is in the private-chat BotFather menu (it wasn't before)",
          'command="start"' in window)


def main():
    print("phase 11 — docs, commands, test harness\n")
    for fn in (
        test_dead_debug_stub_removed,
        test_consolidated_runner_exists,
        test_readme_reflects_current_architecture,
        test_env_example_documents_main_account_id,
        test_setmain_command_name_is_correct_everywhere,
        test_start_command_is_advertised_to_botfather,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 11 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 11 checks passed")


if __name__ == "__main__":
    main()
