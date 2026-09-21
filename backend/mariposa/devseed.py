"""开发环境种子：生成/读取 runtime/dev_tokens.json 并写入绑定。

token 明文只落 runtime/（不入 git）；库中仅存 sha256。
用法：python -m mariposa.devseed [--ensure]
"""
from __future__ import annotations

import json
import sys

from . import config, schema
from .identity import service as identity


def ensure_tokens() -> dict[str, str]:
    path = config.RUNTIME_DIR / "dev_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = identity.generate_dev_tokens()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"generated dev tokens -> {path}")
    return data


def main() -> int:
    schema.migrate()
    tokens = ensure_tokens()
    identity.seed(tokens)
    print("seeded principals/bindings: " + ", ".join(sorted(tokens)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
