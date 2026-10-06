"""Qwen3-Embedding-4B（MLX 4bit-DWQ）本地语义 provider。

2026-09-30 江乔生批准部署 4B。纯本地推理：不联网、不外发文本。

- 权重：mlx-community/Qwen3-Embedding-4B-4bit-DWQ（约 2.5GB）
- 查询侧带官方 instruct 检索模板；文档（语料）侧原样编码
- 输出 2560 维全维 + L2 归一化（余弦=点积）
- last-token 池化（官方语义）；批量编码控峰值内存
- 懒加载：首次 embed 时载入（常驻约 2.5GB 统一内存）
"""
from __future__ import annotations

import os

MODEL_DIRNAME = "qwen3-embedding-4b-4bit"
DIM = 2560
MAX_LENGTH = 2048
BATCH = 8

#: Qwen3-Embedding 官方检索任务指令（查询侧）
INSTRUCT = ("Given a web search query, retrieve relevant passages "
            "that answer the query")

_state = {"model": None, "tokenizer": None}


def model_root() -> str:
    """权重位置：默认代码仓库 runtime/models/（与 gitignore 布局一致），
    可用 MARIPOSA_QWEN_EMBED_MODEL_DIR 显式覆盖（生产数据根独立时）。"""
    env = os.environ.get("MARIPOSA_QWEN_EMBED_MODEL_DIR")
    if env:
        return env
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))  # .../mariposa
    return os.path.join(repo, "runtime", "models", MODEL_DIRNAME)


def available() -> bool:
    return (os.path.isfile(os.path.join(model_root(),
                                        "model.safetensors"))
            and os.path.isfile(os.path.join(model_root(),
                                            "tokenizer.json")))


def _load():
    if _state["model"] is not None:
        return
    import mlx.core as mx
    from mlx_lm import load
    model, tokenizer = load(model_root())
    model.eval()
    _state["model"] = model
    _state["tokenizer"] = tokenizer


def _last_token_pool(hidden, attention_mask):
    """官方池化：每序列最后一个有效 token 的 hidden。"""
    import mlx.core as mx
    # (B, L)；每个序列右侧 padding，最后有效位 = mask 和 - 1
    idx = attention_mask.sum(axis=1) - 1  # (B,)
    rows = hidden.shape[0]
    return hidden[mx.arange(rows), idx]


def _encode(texts: list[str]) -> "list":
    import mlx.core as mx
    _load()
    tok = _state["tokenizer"]
    model = _state["model"]
    out = []
    for i in range(0, len(texts), BATCH):
        batch = [t if t.strip() else " " for t in texts[i:i + BATCH]]
        inner = getattr(tok, "_tokenizer", None) or tok
        enc = inner(batch, padding=True, truncation=True,
                    max_length=MAX_LENGTH, return_tensors="np")
        ids = mx.array(enc["input_ids"].tolist())
        mask = mx.array(enc["attention_mask"].tolist())
        # 主干 forward：hidden states（过 lm_head 之前）
        hidden = model.model(ids)
        pooled = _last_token_pool(hidden, mask)
        # L2 归一化：余弦相似度=点积
        normed = pooled / mx.sqrt((pooled * pooled).sum(-1,
                                                        keepdims=True))
        normed = normed.astype(mx.float32)  # bf16 → f32（numpy 可转）
        out.extend([mx.reshape(v, (-1,)) for v in normed])
    return out


def embed(texts: list[str]):
    """文档/语料侧编码（不加 instruct）。返回 mlx 数组列表。"""
    import numpy as np
    return [np.asarray(v, dtype=np.float32) for v in _encode(texts)]


def embed_query(query: str):
    """查询侧编码（官方 instruct 模板）。"""
    import numpy as np
    q = f"Instruct: {INSTRUCT}\nQuery: {query}"
    return [np.asarray(v, dtype=np.float32) for v in _encode([q])][0]

