# Qwen3-Embedding-4B 本地部署（2026-09-30 江乔生批准）

## 决策记录

- 选型：本地 MLX（Apple Silicon）而非云端 API——embedding 工种
  参数在几个 B 即到顶（云端无 27B 级可用也不需要），本地 4B 已是
  中文第一梯队且零外发零费用；
- 权重：mlx-community/Qwen3-Embedding-4B-4bit-DWQ（2.26GB，
  runtime/models/qwen3-embedding-4b-4bit/，gitignore 内）；
- 部署位置：M6 24GB，常驻约 2.5GB 统一内存（余量 58% 实测）。

## 实现

- `retrieval/qwen_embed.py`：MLX provider——mlx-lm 加载主干、
  last-token 池化（官方语义）、L2 归一化、查询侧官方 instruct
  模板 / 语料侧原样、批量编码、懒加载（实测加载 1.1s、热编码
  0.06s/条、2560 维全维）。
- `semantic.py` 接入：`SEMANTIC_PROVIDER=qwen3e4b` 新值；
  LOCAL_PROVIDERS 白名单集中（reindex/search/dense-tail/
  words 全部统一）；模型身份/维度/阈值随 provider 切换
  （_active_model_key/dim/threshold）——换模型 = 向量身份换代，
  旧 bge 向量自动失效不混用（测试钉住）；provider 实例类型守卫
  防跨测试状态串台。
- 阈值校准（合成对，2026-09-30）：同义改写 0.42–0.65、无关
  0.17–0.29、间隔 +0.13 → **初始阈值 0.35**
  （MARIPOSA_QWEN_SEMANTIC_THRESHOLD 可调；不盲搬 bge 的
  0.51——两模型分数分布不同）。

## 实测数据

- 全链路（隔离根）：字面零重叠查询"那晚夜空中的繁星银河"经
  dense 命中星空记忆（matched_by=semantic）；禁检来源
  （meaning）零命中；向量身份 Qwen/...|eventbody-v1 dim=2560。
- 双模型对照（10 记忆 × 10 改写查询，均零字面重叠）：
  Hit@3 双 10/10（题面区分度不足）。
- **近义干扰分离度**（关键差异）：目标 vs 最强干扰的分数差——

  | 查询场景 | bge-small | Qwen3-4B |
  |---|---|---|
  | 星空（vs 天文馆） | +0.604 | +0.573 |
  | 海岸线傍晚（vs 海边民宿清晨） | **+0.050** | **+0.641** |
  | 汤香（vs 汤面馆） | +0.582 | +0.603 |

  难例（主题相近、细节区分）上分离度 12 倍——这正是 4B 的价值。

## 生产启用

config 默认未启用（代码就绪）。生产切换 = 设置
`MARIPOSA_SEMANTIC_PROVIDER=qwen3e4b` + warmup（首次全量重嵌，
几分钟）。真实语料阈值复核与质量对照（WP07）待她出题后执行。
