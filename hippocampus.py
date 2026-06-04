import json

# 读取文件
with open("test.md", "r", encoding="utf-8") as f:
    content = f.read()

# 取前500个字符
preview = content[:500]

# 生成结果
result = {
    "preview": preview
}

# 写入json
with open("output.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print("Done!")