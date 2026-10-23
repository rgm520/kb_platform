# NAS 知识库流水线（kb）

把 NAS 上的文档变成可问答的知识库：**解析 → 分块 → LLM 知识提炼 → 向量化 → 知识图谱 → Wiki → 混合检索问答**。

全部跑在 AI 服务器（i5-12400F + RTX 3060 12GB）上，NAS 只做存储。

---

## 组件

| 文件 | 职责 |
|---|---|
| `parsers.py` | PDF/docx/xlsx/pptx/ofd/txt → 文本块 + 结构化元数据；PDF 自动区分文本型/扫描件 |
| `chunker.py` | 按文档结构分块（页/段/表/幻灯片），保留定位信息（`p5`、`sheet:xxx`） |
| `llm.py` | Ollama 封装：JSON Schema 约束生成 + JSON 修复重试 + 批量向量化 |
| `store.py` | SQLite：文件指纹、文本块、向量(fp16)、抽取结果、图谱节点/边 |
| `graph.py` | 建图、边合并、实体归一（向量聚类 + LLM 复核）、提及次数重算 |
| `ingest.py` | 增量摄取主程序 |
| `query.py` | 混合检索（向量 + BM25 + 图谱扩展 + wiki 层）+ 问答生成 |
| `export_wiki.py` | 导出 `knowledge.jsonl` / `graph.json` / `wiki/*.md` |

---

## 用法

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
cd D:\kb-platform

# 1) 摄取（增量：内容未变的文件自动跳过）
& $py -m kb.ingest "W:\AI知识库" --db D:/kb-platform/kb/data/kb.db

# 强制重跑
& $py -m kb.ingest "W:\AI知识库" --db D:/kb-platform/kb/data/kb.db --force

# 2) 导出 Wiki（写到 NAS 的 _kb/ 目录）
& $py kb\export_wiki.py --db D:/kb-platform/kb/data/kb.db --out "W:\AI知识库\_kb"

# 3) 问答
& $py -m kb.query "三级及以下子企业能不能自建数据中心机房？" --db D:/kb-platform/kb/data/kb.db --wiki "W:\AI知识库\_kb\wiki"
```

---

## 关键设计决策（都是实测踩出来的）

### 1. 必须用 `qwen3.5-9b-64k`，不能用 `qwen3.5:9b`

Ollama 默认 runner 的 `context_length` 只有 **4096**，超出的部分**静默截断且不报错**。
已建好变体：

```powershell
"FROM qwen3.5:9b`nPARAMETER num_ctx 65536`n" | Out-File Modelfile -Encoding utf8
ollama create qwen3.5-9b-64k -f Modelfile
```

32K 变体占 6.13GB 显存，64K 占 7.05GB（12GB 卡），bge-m3 占 0.62GB。

### 2. 必须用 JSON Schema 约束，否则 40% 输出无法解析

同一提示词重复 5 次实测：

| 模式 | JSON 合法率 |
|---|---|
| 无约束 | 2/5 |
| `format=json` | 4/5 |
| `format=<JSON Schema>` | **5/5** |

注意 `format` 要传 **JSON 对象**，传字符串会报 `invalid format`。
另：`num_predict` 必须放大（默认 900 会 `done_reason=length` 截断）。

### 3. 结构化抽取必须 `think=false`

thinking 模式会吃掉输出预算（实测 thinking 占 3320 字符），导致 JSON 被截断。

### 4. 实体归一不能全靠 LLM

先用归一化名称精确合并，再用 bge-m3 向量聚类找候选对，**只把候选对交给 LLM 复核**。
实测单份文档就正确合并出「垂直领域数字化 / 垂直领域数字化项目」这类同义实体。

### 5. 图谱的边方向不可靠

同一文档两次抽取可能给出相反的 from/to（如 `采购评审小组 --组成成员--> 物资部门`
vs `物资部门 --组成成员--> 采购评审小组`）。**边的方向与数量都需要后处理归一**。

### 6. 图谱不是主检索通道

图谱召回覆盖率低，作用是①补全向量检索漏掉的实体关系②组织答案结构。
主力是「BM25 关键词 + 向量」混合检索——中文企业文档全是精确术语、编号、金额，
纯向量检索会漏。BM25 用**中文字符二元组**分词，无需额外分词器。

### 7. Wiki 是人工校正层，必须参与检索

`_kb/wiki/entities/*.md` 可直接编辑。检索时 wiki 页面向量加权 1.8x，
并保证至少 1 个 wiki 页进入结果集——否则人工改过的页面永远检索不到。
wiki 的内容会作为「片段」引用到答案里。

---

## 性能基线（RTX 3060 12GB 实测）

| 指标 | 值 |
|---|---|
| Prefill | ~1,600 tok/s |
| Decode | ~46 tok/s |
| 抽取单块（2000 tok 提示 + 1400 tok 输出） | ~30s |
| bge-m3 向量化 | 0.07s/段（批量） |
| 问答（8 问平均） | 13.2s/问 |
| 首 token 延迟 | ≈ prefill 时间 |

---

## 已知限制

- **扫描件 PDF 未接 OCR**。`parsers.py` 会标记 `need_ocr=True`，需要接 PaddleOCR 或走 vision 管线。
- **老格式 .doc/.ppt/.xls 未支持**，需 LibreOffice 转换（NAS 上共 21,833 个 .doc）。
- **.ofd 只做了粗粒度文本抽取**，需 ofdparser 做精确版式还原。
- **向量全量载入内存**。当前规模无碍；若全量回填 28 万份文档（约 80 万块 × 1024 维 fp16 ≈ 1.6GB），
  建议改为分片索引或换专用向量库。
- **边方向未归一**，`graph.py` 里缺这一步。
