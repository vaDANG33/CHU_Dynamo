"""Update only the embedded MUR_01 code, preserving the Dynamo JSON layout."""
import json
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "Mur_DWG-v3.dyn"
source = path.read_bytes().decode("utf-8")
graph = json.loads(source)
node = next(n for n in graph["Nodes"] if n["Id"] == "85c5a7bf21fc4c6f9f26805b52d6b861")
code = (Path(__file__).parent / "mur01.py").read_text(encoding="utf-8")
old = json.dumps(node["Code"], ensure_ascii=False)
assert source.count(old) == 1, "Embedded code must have a unique serialized representation"
updated = source.replace(old, json.dumps(code, ensure_ascii=False), 1)
check = json.loads(updated)
node["Code"] = code
assert check == graph, "Unexpected graph change"
path.write_bytes(updated.encode("utf-8"))
