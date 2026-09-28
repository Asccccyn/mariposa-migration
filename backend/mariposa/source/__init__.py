"""Source Layer（原文层）：Raw Archive → Adapter → Normalized Store → Binding。

原文是证据层，不是摘要层：母本只读保存在 runtime/source/raw/；
标准化副本可由母本重建；Memory 只引用原文（memory_source_bindings），
不复制原文。普通 Recall 不扫描原文（专项检索走 source.query）。
"""
