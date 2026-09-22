# 导出能力契约清单：contracts/capabilities.v1.json（与代码同源生成）
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
from mariposa import schema as _schema  # noqa: E402
_schema.migrate()
from mariposa.capabilities import v1_compat  # noqa: E402
v1_compat.register_v1_compat()
from mariposa.capabilities.registry import REGISTRY  # noqa: E402

out = {
    "contract_version": "1.0",
    "generated_from": "backend/mariposa/capabilities/registry.py",
    "capabilities": [
        {
            "canonical_name": name,
            "transport_name": "mariposa_" + name.replace(".", "_"),
            "allowed_principals": sorted(cap.allowed_principals),
            "write": cap.write,
            "idempotent": cap.idempotent,
            "description": cap.description,
        }
        for name, cap in sorted(REGISTRY.items())
    ],
}

dest = Path(__file__).parents[1] / "contracts" / "capabilities.v1.json"
dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"exported {len(out['capabilities'])} capabilities -> {dest}")
