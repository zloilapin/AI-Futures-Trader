import os
import re
import glob

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BLOCKS_DIR = os.path.join(BASE_DIR, "gemini_audit_blocks")

def update_block_file(block_path):
    with open(block_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Pattern for code blocks:
    # <a id="..."></a>\n## File: `path/to/file` (N строк, X.X KB)\n\n```python\n...\n```
    # We find sections starting from '## File: `...' to the next '## File: `' or end of file.
    
    file_pattern = re.compile(r"(## File: `([^`]+)`[^\n]*\n\n```(?:python|html|txt|json)?\n)(.*?)(```)", re.DOTALL)

    def replacer(match):
        prefix = match.group(1)
        rel_path = match.group(2)
        code_body = match.group(3)
        suffix = match.group(4)

        disk_path = os.path.join(BASE_DIR, rel_path.replace("/", os.sep))
        if not os.path.exists(disk_path):
            print(f"  [WARN] File not found on disk: {disk_path}")
            return match.group(0)

        with open(disk_path, "r", encoding="utf-8") as df:
            disk_content = df.read()

        line_count = len(disk_content.splitlines())
        size_kb = len(disk_content.encode("utf-8")) / 1024.0

        # Replace line count and KB in prefix
        new_prefix = re.sub(
            r"## File: `([^`]+)` \(\d+ строк, [\d.]+ KB\)",
            f"## File: `{rel_path}` ({line_count} строк, {size_kb:.1f} KB)",
            prefix
        )
        if not disk_content.endswith("\n"):
            disk_content += "\n"

        return f"{new_prefix}{disk_content}{suffix}"

    updated = file_pattern.sub(replacer, content)

    if updated != content:
        with open(block_path, "w", encoding="utf-8") as f:
            f.write(updated)
        print(f"Updated {os.path.basename(block_path)}")
    else:
        print(f"No changes in {os.path.basename(block_path)}")

def update_all_prompts():
    prompt_md_path = os.path.join(BLOCKS_DIR, "ALL_AGENT_PROMPTS.md")
    if not os.path.exists(prompt_md_path):
        return

    with open(prompt_md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Pattern: ## <num>. Промпт: `filename.txt` (N строк)\n\n```txt\n...\n```
    pattern = re.compile(r"(## \d+\. Промпт: `([^`]+)`[^\n]*\n\n```txt\n)(.*?)(```)", re.DOTALL)

    def replacer(match):
        prefix = match.group(1)
        filename = match.group(2)
        code_body = match.group(3)
        suffix = match.group(4)

        disk_path = os.path.join(BASE_DIR, "python", "prompts", filename)
        if not os.path.exists(disk_path):
            print(f"  [WARN] Prompt file not found: {disk_path}")
            return match.group(0)

        with open(disk_path, "r", encoding="utf-8") as df:
            disk_content = df.read()

        line_count = len(disk_content.splitlines())
        new_prefix = re.sub(
            r"## (\d+\. Промпт: `[^`]+`) \(\d+ строк\)",
            f"## \\1 ({line_count} строк)",
            prefix
        )
        if not disk_content.endswith("\n"):
            disk_content += "\n"

        return f"{new_prefix}{disk_content}{suffix}"

    updated = pattern.sub(replacer, content)
    if updated != content:
        with open(prompt_md_path, "w", encoding="utf-8") as f:
            f.write(updated)
        print("Updated ALL_AGENT_PROMPTS.md")
    else:
        print("No changes in ALL_AGENT_PROMPTS.md")

def update_crash_file():
    crash_path = os.path.join(BASE_DIR, "crash.md")
    if not os.path.exists(crash_path):
        return
    with open(crash_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Update sentinel_agent.py tail
    sentinel_path = os.path.join(BASE_DIR, "python", "agents", "sentinel_agent.py")
    ptp_path = os.path.join(BASE_DIR, "python", "services", "partial_tp_calculator.py")

    with open(sentinel_path, "r", encoding="utf-8") as f:
        sentinel_lines = f.readlines()
    tail_216 = "".join(sentinel_lines[215:])

    with open(ptp_path, "r", encoding="utf-8") as f:
        ptp_content = f.read()

    # Reconstruct crash.md
    new_crash = (
        "# CRASH AUDIT EXTENSION: BLOCK_06 TAIL (UPDATED)\n\n"
        "## sentinel_agent.py (from line 216 to end)\n"
        "```python\n"
        f"{tail_216}```\n\n"
        "---\n\n"
        "## partial_tp_calculator.py (FULL FILE)\n"
        "```python\n"
        f"{ptp_content}```\n"
    )

    with open(crash_path, "w", encoding="utf-8") as f:
        f.write(new_crash)
    print("Updated crash.md")

if __name__ == "__main__":
    for b_path in sorted(glob.glob(os.path.join(BLOCKS_DIR, "BLOCK_*.md"))):
        update_block_file(b_path)
    update_all_prompts()
    update_crash_file()

