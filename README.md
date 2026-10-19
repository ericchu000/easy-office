# Easy Office

本地多模态智能办公助手。当前已完成**步骤一：工程底座、动态配置中心、双模式文件存储抽象与分层元数据存储**。

## 已实现能力

- **动态配置中心**：Pydantic 强类型校验、YAML 原子持久化、密钥脱敏、观察者热重载、五类模型配置与连通性探测。
- **双模式文件存储**：宿主机原地引用（零拷贝）与跨端上传托管归档；SHA-256、MIME、时间戳等第一层属性采集；移动/复制/流式读取。
- **分层元数据**：SQLite WAL 元数据主表、第二层语义增强元数据回填接口、三类模态专属结构摘要、标签倒排表与多条件复合筛选。
- **混合索引**：SQLite FTS5（jieba 中文分词 + BM25）与 sqlite-vec 稠密向量索引，支持元数据联合过滤。
- **纯文本伴生镜像缓存**：按文件 UID 生成 `.txt` 镜像，每行携带文件标识、页码/工作表/行列前缀，可反向映射回源文件。

## 环境

```powershell
cd D:\code\easy-office
uv sync
```

Python 3.11+。依赖：`pydantic`、`PyYAML`、`sqlite-vec`、`jieba`、`pytest`。

SQLite 需启用 FTS5 与扩展加载（CPython Windows 官方 3.11 已满足）。

## 快速开始

```python
from easy_office.core import EasyOfficeCore

core = EasyOfficeCore.bootstrap()          # 默认数据目录 <项目>/data
record = core.register_inplace(r"D:\资料\报告.docx")
upload = core.ingest_upload("客户名单.csv", open(r"C:\temp\客户名单.csv", "rb"))
print(record.file_uid, upload.storage_mode)
core.close()
```

首次启动会自动生成 `data/config.yaml`、`data/index/knowledge.db`、`data/managed/`、`data/mirror/`。

## 测试

```powershell
# 第二档：开发者底层测试（29 项）
.venv\Scripts\python.exe -m pytest tests\step_01_technical_test.py -v

# 第一档：业务链路演示（人类审查）
.venv\Scripts\python.exe tests\step_01_business_walkthrough.py
# 追加 --keep 保留临时数据目录以便检查数据库与镜像文件
```

## 目录

```text
easy_office/
  config/       配置模型与管理器
  storage/      双模式文件存储抽象与本地驱动
  metadata/     分层元数据模型与 SQLite 仓库
  db/           数据库连接、Schema、WAL 与 sqlite-vec
  indexing/     分词、FTS5/向量混合索引、伴生镜像缓存
  core.py       服务组合根
tests/          步骤一业务演示与底层测试
docs/           开发计划与开发总结
```
