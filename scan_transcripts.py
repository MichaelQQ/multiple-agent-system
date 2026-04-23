import json, os, re
from pathlib import Path
from collections import Counter

projects_dir = Path.home() / ".claude" / "projects"
if not projects_dir.exists():
    print("No projects dir found")
    exit()

all_jsonl = sorted(projects_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
print(f"Scanning {len(all_jsonl)} transcript files")

bash_cmds = Counter()
mcp_tools = Counter()

def leading_tokens(cmd):
    cmd = cmd.strip()
    cmd = re.sub(r'^(?:[A-Z_]+=\S+\s+)+', '', cmd)
    cmd = re.sub(r'^(sudo|timeout\s+\S+)\s+', '', cmd)
    tokens = cmd.split()
    if not tokens:
        return None
    prog = tokens[0]
    for sep in ('&&', '||', '|', ';'):
        if sep in cmd:
            first = cmd.split(sep)[0].strip()
            first = re.sub(r'^(?:[A-Z_]+=\S+\s+)+', '', first)
            first = re.sub(r'^(sudo|timeout\s+\S+)\s+', '', first)
            tokens2 = first.split()
            if tokens2:
                prog = tokens2[0]
                tokens = tokens2
            break
    subcommand_tools = {'git', 'gh', 'docker', 'kubectl', 'npm', 'yarn', 'pnpm', 'bun', 'pip', 'pip3', 'cargo'}
    if prog in subcommand_tools and len(tokens) > 1:
        return f"{prog} {tokens[1]}"
    return prog

for f in all_jsonl:
    try:
        for line in f.read_text(errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get('type') != 'assistant':
                continue
            msg = obj.get('message', {})
            for item in msg.get('content', []):
                if item.get('type') != 'tool_use':
                    continue
                name = item.get('name', '')
                inp = item.get('input', {})
                if name == 'Bash':
                    cmd = inp.get('command', '')
                    key = leading_tokens(cmd)
                    if key:
                        bash_cmds[key] += 1
                elif name.startswith('mcp__'):
                    mcp_tools[name] += 1
    except Exception:
        pass

print("\n=== Top Bash commands ===")
for cmd, count in bash_cmds.most_common(40):
    print(f"  {count:4d}  {cmd}")

print("\n=== Top MCP tools ===")
for tool, count in mcp_tools.most_common(20):
    print(f"  {count:4d}  {tool}")
