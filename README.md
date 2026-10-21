# Easy Office

本地多模态智能办公助手。当前已完成**步骤一：工程底座、动态配置中心、双模式文件存储抽象与分层元数据存储**，以及**步骤二：多源版面办公文档统一解析框架、内嵌图表表格还原与结构化切片管道**。

## 已实现能力

- **动态配置中心**：Pydantic 强类型校验、YAML 原子持久化、密钥脱敏、观察者热重载、五类模型配置与连通性探测。
- **双模式文件存储**：宿主机原地引用（零拷贝）与跨端上传托管归档；SHA-256、MIME、时间戳等第一层属性采集；移动/复制/流式读取。
- **分层元数据**：SQLite WAL 元数据主表、第二层语义增强元数据回填接口、三类模态专属结构摘要、标签倒排表与多条件复合筛选。
- **混合索引**：SQLite FTS5（jieba 中文分词 + BM25）与 sqlite-vec 稠密向量索引，支持元数据联合过滤。
- **纯文本伴生镜像缓存**：按文件 UID 生成 `.txt` 镜像，每行携带文件标识、页码/工作表/行列前缀，可反向映射回源文件。
- **办公文档解析（步骤二）**：Docling 可插拔后端、DOCX/PPTX 原生解析、DOC/PPT 经 LibreOffice 定版转 PDF、PDF 逐页数字/扫描智能分流、公式 LaTeX 富化、办公文档结构摘要回填。
- **表格与图片结构还原（步骤二）**：零 Token 的 HTML/Markdown 自适应表格转换（保留 `rowspan`/`colspan`）、装饰图确定性过滤、图片裁剪 OCR 与 VLM 描述、章节/页码/坐标框切片。

## 环境

```powershell
cd D:\code\easy-office
uv sync
```

Python 3.11+。依赖：`pydantic`、`PyYAML`、`sqlite-vec`、`jieba`、`docling[rapidocr]`、`pymupdf`、`pytest`。

SQLite 需启用 FTS5 与扩展加载（CPython Windows 官方 3.11 已满足）。

步骤二额外需要：

1. **LibreOffice**（处理 `.doc` / `.ppt`）：<https://www.libreoffice.org/download/>
2. **Docling 模型权重**：

```powershell
.venv\Scripts\python.exe scripts\prefetch_docling_models.py
```

如访问 Hugging Face 困难，可先设置 `$env:HF_ENDPOINT="https://hf-mirror.com"`。

## 快速开始

```python
from easy_office.core import EasyOfficeCore

core = EasyOfficeCore.bootstrap()          # 默认 config.yaml + <项目>/runtime
record = core.register_inplace(r"D:\资料\报告.docx")
upload = core.ingest_upload("客户名单.csv", open(r"C:\temp\客户名单.csv", "rb"))
print(record.file_uid, upload.storage_mode)

# 步骤二：解析、切片、回填办公摘要并写入索引/镜像
result = core.office.ingest(record.file_uid)
print(result.chunk_count, result.summary)

core.close()
```

首次启动会在项目根生成 `config.yaml`，并创建 `runtime/`（内含 `knowledge.db`、`mirror/`、`assets/`、`tmp/`）、`managed_src_files/` 与 `models/`。`data/` 仅用于存放待索引的源文档；它不是运行时目录。

Embedding 与 VLM 使用任意 OpenAI 兼容服务（也支持 Ollama 的 `/v1` 接口），在项目根 `config.yaml` 的 `embedding` / `vlm` 块中配置；未配置时文本索引仍可用，图片描述自动降级为 OCR + 尺寸说明。

## 测试

测试使用真实文档与真实模型服务；请先在项目根 `config.yaml` 配置 Embedding/VLM，并在 `models/` 下载 Docling 权重。

```powershell
# 默认：纯逻辑测试 + 真实文档上的 Docling/PDF 探针（不访问外部模型）
.venv\Scripts\python.exe -m pytest tests -q

# 真实 Embedding / VLM / 连通性测试
$env:EASYOFFICE_REAL_SERVICES="1"
.venv\Scripts\python.exe -m pytest tests -q -k "real_"

# 步骤一：真实文档 + 真实模型的完整业务链路
.venv\Scripts\python.exe tests\step_01_business_walkthrough.py data --query 功率预测

# 步骤二：真实文档解析、OCR、VLM、Embedding 与检索业务链路
.venv\Scripts\python.exe tests\step_02_business_walkthrough.py data --query 功率预测

# 真实文档全量解析验收
$env:EASYOFFICE_STEP2_DOCS="D:\code\easy-office\data"
.venv\Scripts\python.exe -m pytest tests\step_02_technical_test.py::test_real_documents_ingest -v
```

默认 `pytest tests -q` 为 `55 passed, 5 skipped`；5 个跳过项分别需要 `EASYOFFICE_REAL_SERVICES=1`、`EASYOFFICE_STEP2_DOCS` 和已下载的 Docling 权重。业务脚本会在配置缺失时直接报错，不会用假模型或合成数据代替。

## 目录

```text
easy_office/
  config/       配置模型与管理器
  storage/      双模式文件存储抽象与本地驱动
  metadata/     分层元数据模型与 SQLite 仓库
  db/           数据库连接、Schema、WAL 与 sqlite-vec
  indexing/     分词、FTS5/向量混合索引、伴生镜像缓存
  models/       Embedding / VLM / RapidOCR 适配器
  parsing/      Docling 驱动、PDF 分流、LibreOffice、表格/图片/分块/摘要/流水线
  core.py       服务组合根
tests/          各步骤业务演示与底层测试
scripts/        Docling 模型预下载与布局迁移脚本
docs/           开发计划与开发总结

config.yaml           用户配置（含密钥，已 gitignore）
models/               Docling 权重缓存（已 gitignore）
data/                 可选的 inplace 源文档目录（已 gitignore）
managed_src_files/    跨端上传的托管源文件（持久，已 gitignore）
runtime/              可删除重建的派生状态（已 gitignore）
  knowledge.db
  mirror/
  assets/
  tmp/
```
