import json

try:
    with open(r"C:\Users\Admin\.gemini\antigravity-ide\brain\1d4738b1-c673-4a32-8de5-09443440e405\.system_generated\steps\1393\content.md", "r", encoding="utf-8") as f:
        data = f.read()
    
    # It might be wrapped in markdown code blocks
    if "```json" in data:
        data = data.split("```json")[1].split("```")[0]
        
    js = json.loads(data)
    
    free_models = []
    for model in js.get("data", []):
        prompt_price = model.get("pricing", {}).get("prompt", "0")
        if prompt_price == "0" or prompt_price == "0.0":
            free_models.append(model["id"])
            
    print("Free models found:", len(free_models))
    for m in free_models:
        print(f"- {m}")
        
except Exception as e:
    print("Error:", e)
