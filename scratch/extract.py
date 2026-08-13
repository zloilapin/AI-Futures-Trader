import json, re

with open(r'C:\Users\Admin\.gemini\antigravity-ide\brain\dc875f6b-f4d9-47d9-8c41-d499cbf78c04\.system_generated\steps\484\content.md', 'r', encoding='utf-8') as f:
    content = f.read()

parts = re.findall(r'\"(?:text|value|parts|content)\":\s*(\[.*?\]|\".*?\")', content)
for p in parts:
    if len(p) > 20 and '\\n' in p:
        print('---')
        # Simple unescape
        try:
            val = json.loads(p)
            if isinstance(val, list):
                val = " ".join(val)
            print(val[:800])
        except:
            pass
