## 步骤一：工程底座、动态配置中心、双模式文件存储抽象与分层元数据存储

### 1）开发内容解读（总分结构）

本步骤从空仓库开始，建立了后续所有步骤共用的 Python 工程底座。代码以 `easy_office` 包组织，自下而上分为五层：`config` 负责可热更新的全局配置，`storage` 负责宿主机原地引用与跨端上传托管两种文件存储模式，`metadata` 与 `db` 负责第一层/第二层元数据及三类模态专属结构摘要的持久化与复合筛选，`indexing` 负责 FTS5 全文索引、sqlite-vec 向量索引和纯文本伴生镜像缓存，最上层的 `core.py` 把这些组件装配成统一的门面对象 `EasyOfficeCore`。此外建立了 `tests/` 下的两档测试与 `README.md` 使用说明。

配置中心由 `easy_office/config/models.py` 与 `easy_office/config/manager.py` 两部分组成。模型部分用嵌套 Pydantic 模型定义了 LLM、VLM、Embedding、Reranker、本地解析权重五类模型配置，以及存储路径、并发、外部消息网关和四类 IM 平台凭证块；所有模型都禁止未知字段，路径、URL、设备枚举、温度、向量维度、批处理大小、端口等参数都有强类型边界校验。管理器部分实现配置文件首次自动生成、YAML 加载、运行时增量更新、敏感密钥脱敏导出、原子持久化、线程安全读写、观察者热重载通知，以及面向 LLM/VLM/Embedding/Reranker/本地解析权重的结构化连通性探测。配置中的相对路径统一相对于数据目录解析，并允许通过 `EASYOFFICE_HOME` 改变默认数据根目录。

双模式对象存储由 `easy_office/storage/base.py` 与 `easy_office/storage/local.py` 实现。抽象基类 `FileStorage` 声明了文件注册、流式上传落盘、流式读取、本地路径定位、SHA-256 计算、复制、移动、删除和存在性判断等契约，并预留了未来 MinIO 等对象存储驱动的替换点；`StoredObject` 统一描述文件的唯一标识、URI、本地路径、存储模式、名称、扩展名、MIME、字节大小、父目录、内容哈希、创建/修改时间和相对路径。默认本地驱动把“宿主机原地引用模式”实现为零拷贝登记，把“跨端上传托管归档模式”实现为按文件 UID 分目录流式落盘；路径白名单在配置为空时不限制，配置后严格限制在白名单根目录内，所有路径都做环境变量展开、绝对化和符号链接解析。

分层元数据由 `easy_office/db/schema.py`、`easy_office/db/database.py` 与 `easy_office/metadata/` 组成。SQLite 以 WAL 模式打开，`files` 主表承载第一层全部系统属性与第二层语义增强字段，并用一个 `modality_summary_kind` 加 JSON 摘要列保证每个文件只挂接办公文档、图像或表格中的一类模态专属摘要；`tags` 与 `file_tags` 两张表承载主题标签、关键词、命名实体、视觉标签与表头列名的倒排关联；`chunks` 明细表记录切片顺序、页码、工作表、行列跨度、版面坐标框、内嵌图缓存路径、结构化原文和去标签纯文本。仓库层 `MetadataStore` 提供文件注册与同路径刷新、语义元数据回填、三类模态摘要回填、标签替换、多条件联合查询、基于 SHA-256 的路径同步、统计报表和删除能力，并在文件类型、修改时间、大小、主题标签、业务时间范围等常用组合上建立了单列与复合索引。

混合索引由 `easy_office/indexing/` 实现。`base.py` 定义了 `ChunkInput`、`ChunkRecord`、`SearchHit` 以及可插拔的 `HybridIndexStorage` 抽象接口；`tokenizer.py` 使用 jieba 对中英文混合文本做分词，再以空格连接后写入 FTS5 的 `unicode61` 表，解决 SQLite 默认分词器把连续中文当成一个 token 的问题；`sqlite_store.py` 用 `chunk_id` 同时对齐 `chunks` 关系表、FTS5 虚表和 sqlite-vec 向量表的 rowid，支持批量写入、按文件清理、BM25 检索、带元数据过滤的向量近邻检索和按页码范围读取切片；`mirror.py` 为每个二进制源文件维护按 UID 命名的 `.txt` 伴生镜像，每行都带文件标识与页码/工作表/行列前缀，并提供反向映射到源文件路径和位置的方法。

`easy_office/core.py` 是组合根。它按“加载配置 → 创建目录 → 打开数据库 → 装配元数据/镜像/索引/存储 → 注册配置校验器与热重载监听器”的顺序启动，并对配置变化做出实际动作：数据库路径变化时切换连接，向量维度变化时校验并按需重建向量表，托管工作区、白名单、上传上限和镜像目录变化时热更新对应组件。门面对象还提供原地登记、上传登记、外部移动后的哈希路径同步、受控移动、删除、切片写入和两路检索的便捷方法，供后续步骤直接复用。

#### 核心代码批注

配置更新的关键路径在 `ConfigManager._validate_and_commit`。更新时先把补丁中的脱敏占位符剔除，再把补丁深合并到当前配置的原始字典中，交给 Pydantic 做整树校验；校验通过后先执行注册的自定义校验器（例如向量维度守卫），再原子写回 YAML，最后才替换内存中的配置并通知监听器。这个顺序保证了“任何一步失败都不会留下半更新的配置或半写入的文件”。密钥导出的处理是：内部保存真实值，`public_config()` 递归识别所有 `SecretStr` 字段并替换为固定的 `********`；当客户端把这份脱敏配置原样提交回来时，掩码占位符会被识别为“保持原值”，因此不会误清空密钥。

```python
cleaned_patch = _strip_masked(patch, old)
merged = _deep_merge(to_raw_dict(old), cleaned_patch)
new = AppConfig.model_validate(merged)
for validator in list(self._validators):
    validator(old, new)
if persist:
    self._persist(new)
self._config = new
self._notify(listeners, new)
```

双模式存储的关键决策是把“文件的物理归属”与“文件的逻辑描述”分开。原地登记只读取宿主文件并计算属性，不创建副本；上传落盘则把字节流写入 `托管工作区/<uid前两位>/<uid>/<安全文件名>`，边读边算 SHA-256 和字节数，超过上限或写入失败会删除半成品。下面的片段体现了流式落盘与上限保护；原地路径则在 `_check_inplace_allowed` 中做白名单包含判断，空白名单表示不限制。

```python
destination = self._workspace / uid[:2] / uid / clean_name
with open(destination, "xb") as handle:
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > self._max_upload_bytes:
            raise StorageError("upload exceeds the configured limit")
        digest.update(chunk)
        handle.write(chunk)
```

分层元数据的核心设计是“关系列 + JSON 扩展 + 倒排表”三者配合。第一层属性与第二层语义字段是普通列，便于排序和精确过滤；三类模态摘要只保留一个 JSON 列和一个类型列，跨类型写入会被 `ModalitySummaryConflict` 拒绝；主题、关键词、实体、视觉标签和表头列名统一进入 `tags` 倒排表。下面的标签过滤片段用 `EXISTS` 子查询而不是 JOIN，避免一个文件命中多个标签时在结果集中重复出现；模态摘要的字段过滤则用 SQLite JSON1 的 `json_extract` 与 `json_each`，既支持标量字段，也支持数组字段的成员判断。

```sql
EXISTS (
  SELECT 1 FROM file_tags ft JOIN tags t ON t.tag_id = ft.tag_id
  WHERE ft.file_uid = files.file_uid
    AND t.kind = ? AND t.value IN (?, ?)
)
```

混合索引的写入路径把同一个 `chunk_id` 作为三张表的主键或 rowid：关系表保存完整切片元数据，FTS5 保存 jieba 分词后的词流，sqlite-vec 保存 L2 归一化后的稠密向量。这样 BM25 命中、向量命中都能直接回查同一个切片，不需要额外的映射表。下面的片段展示了更新切片时先删除旧 FTS/向量行再重建，确保切片序号相同的“替换”不会留下旧索引；向量检索时先把元数据过滤命中的候选 `chunk_id` 取出来，再通过 `rowid IN (...)` 把向量近邻限制在候选集内。

```python
conn.execute("DELETE FROM chunk_fts WHERE rowid = ?", (chunk_id,))
conn.execute("DELETE FROM chunk_vec WHERE rowid = ?", (chunk_id,))
conn.execute(
    "INSERT INTO chunk_fts (rowid, chunk_uid, file_uid, body) VALUES (?, ?, ?, ?)",
    (chunk_id, chunk_uid, file_uid, normalize_for_index(plain)),
)
if chunk.embedding is not None:
    conn.execute(
        "INSERT INTO chunk_vec (rowid, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32(self._normalize(chunk.embedding))),
    )
```

伴生镜像的格式设计目标是“每一行都能被 ripgrep 直接命中并反向解析”。每行以 `@@EO` 开头，用 `|` 分隔文件 UID、切片类型、起止页码、工作表名、起止行号与起止列号，最后一段是正文；工作表名用百分号编码，正文可能包含 `|` 因此放在最后一个字段。写入时按物理行拆分，表格 HTML 会先剥标签，整个文件通过临时文件加 `os.replace` 原子替换。镜像文件删除、切片清理与切片入库保持同步；切片再次同步时若镜像已关闭，会清掉对应文件的旧镜像，避免 ripgrep 返回不再维护的内容。

```python
@@EO|<file_uid>|<kind>|<page_start>|<page_end>|<sheet>|<row_start>|<row_end>|<col_start>|<col_end>|<text>
```

### 2）完整的测试方法

本步骤的测试环境为 Windows 11、Python 3.11.9、SQLite 3.45.1（已启用 FTS5 与扩展加载）、sqlite-vec 0.1.9、jieba 0.42.1、pytest 9.1.1，并接入了用户提供的真实模型与真实文档：Embedding 为 DashScope 的 `qwen3.7-text-embedding`（1024 维），VLM 为 `deepseek-flash`，文档解析使用 Docling 2.140 与约 1.3 GB 的 `models/` 权重，旧格式转换使用本机 LibreOffice。涉及模型与真实文档的验收全部使用真实 HTTP 服务与用户提供的 `data/` 文件；底层存储/索引单元测试中的单位向量只用于验证 SQL、余弦相似度和维度校验等数学契约，不作为模型能力或业务效果的证据。

第一档是面向人类审查的业务链路演示，脚本为 `tests/step_01_business_walkthrough.py`。它按用户使用产品的顺序串起一条真实故事：启动时读取项目根真实的 `config.yaml`，把运行数据放到临时目录（或 `--data-dir` 指定目录），并通过 `local_parser.weights_dir` 指向真实的 `models/`；随后在线修改 LLM 温度并观察热重载，检查公开配置中的真实密钥已脱敏；把一份真实 DOCX 复制到临时宿主文件夹后以“宿主机原地引用模式”登记，确认只计算 SHA-256、大小和 MIME 而不复制字节；再把一份真实数字版 PDF 的字节流通过“跨端上传托管归档模式”写入临时托管工作区，逐字节比对上传结果；接着分别调用 `core.office.ingest` 让真实 Docling、RapidOCR、VLM 和 Embedding 完成解析、图片描述、切片与向量化；用真实解析结果回填第二层语义元数据、办公结构摘要，并从真实表格切片导出 CSV、从真实 VLM 图片描述复制 PNG，分别回填表格与图像模态摘要；执行“主题标签 + 扩展名 + 模态类型”的复合筛选；用真实 BM25 和真实 1024 维查询向量执行检索；查看伴生镜像的机器可读前缀；最后把文档移动到归档目录并用 SHA-256 路径同步保证数据库路径与磁盘一致，同时验证非法温度配置会被强类型校验拦截且不会污染运行中的配置。

脚本的参数和设置方法如下。第一个位置参数 `docs` 是真实文档目录，默认项目下的 `data`，用于自动寻找一份真实 DOCX 作为原地引用样本、一份带原生文本层的真实 PDF 作为上传样本；可用 `--inplace PATH` 和 `--upload PATH` 明确指定，二者不能相同。`--data-dir PATH` 指定运行数据目录，默认在系统临时目录下创建并在结束时删除；数据库、托管区、镜像区和资产缓存都位于该临时目录，配置文件始终读取项目根真实的 `config.yaml`，因此不会污染正式知识库；脚本会把托管区覆盖到临时目录，避免把演示上传写入正式的 `managed_src_files/`。`--query TEXT` 指定 BM25 与向量检索的查询，默认“功率预测”。`--keep` 保留临时目录，便于人工打开数据库、镜像和资产文件核对。脚本要求项目根 `config.yaml` 中真实 Embedding 与 VLM 均已配置、`models/` 已下载 Docling 权重，否则直接以非零退出码给出明确错误。运行命令为：

```powershell
.venv\Scripts\python.exe tests\step_01_business_walkthrough.py data --query 功率预测
```

实测输出 30 个 `[通过]`，最后打印“演示完成（真实文档 + 真实模型）”并以退出码 0 结束。真实数据结果：原地 DOCX 为流式页语义、29 个切片/29 个向量，上传数字版 PDF 为 5 页、23 个切片/23 个向量；真实 Embedding 生成 1024 维向量，真实 VLM 生成图像视觉标签与描述；BM25 和向量检索均返回相关页面；伴生镜像包含 DOCX 的正文与页码前缀；归档移动后文件 UID、内容哈希和数据库路径保持一致。

这条链路依次调用的组件与接口是：`EasyOfficeCore.bootstrap` 装配配置、存储、元数据、索引和解析流水线；`ConfigManager.get/set/update/subscribe/public_config/paths` 负责读写配置、热重载通知、密钥脱敏和路径解析；`LocalFileStorage.register_inplace/ingest_stream` 与 `EasyOfficeCore.register_inplace/ingest_upload` 负责两种存储模式；`OfficeDocumentPipeline.ingest` 负责真实解析、图片转写、切片和向量化；`MetadataStore.update_semantic_metadata/update_modality_summary/query_files/get_file` 负责两层元数据、三类模态摘要和复合筛选；`SQLiteHybridIndexStore.get_chunks/search_bm25/search_vector` 负责切片读取与两路检索；`MirrorCache.path_for/read_lines` 负责伴生镜像；`EasyOfficeCore.sync_moved_path` 负责哈希路径同步；`build_embedding_client` 负责把查询文本转成真实 1024 维向量。人类审查时只需看脚本打印的业务步骤和 `[通过]`，不需要阅读底层实现。

第二档是面向开发者的底层测试，脚本为 `tests/step_01_technical_test.py`，共收集 29 个测试。配置部分覆盖默认文件自动生成、五类模型与路径配置的合法更新落盘与观察者通知、十类非法配置（温度越界、向量维度为零、批处理为零、非法设备、零进程、非法 URL、未知字段、重排器开关冲突、未知网关平台、端口越界）的无副作用拒绝、密钥脱敏与“脱敏配置回写不丢密钥”、向量维度守卫与清空后的重建。存储部分覆盖两种存储模式的属性准确性、允许白名单与越界拒绝、上传上限与二进制流校验、流式读取、复制、移动、本地路径定位、宿主文件不可被存储层删除、重复登记同路径保持 UID、外部移动后的哈希路径同步、哈希歧义拒绝、内容变化时拒绝路径同步。元数据部分覆盖第二层字段回填、三类模态摘要互斥、视觉标签与表头列名倒排、主题/关键词/实体/扩展名/大小/名称/业务时间/模态字段的复合筛选、业务日期区间与标签类型校验、统计报表。索引部分覆盖中文 BM25、英文 BM25、表格文本命中、向量近邻排序、带元数据过滤的向量检索、页码范围读取、切片替换后旧词失效、维度不匹配拒绝、重复切片序号拒绝、镜像文件生成与逐行解析、表格 HTML 剥标签、镜像反向映射、禁用镜像、切片更新、删除文件时清理切片/镜像/托管字节、内容变化后清理陈旧派生数据。基础设施部分覆盖 WAL、sqlite-vec 版本、复合索引存在性、关闭后重开数据持久化、数据库/托管区/镜像目录热切换，以及不支持的驱动被拒绝。默认运行命令为 `.venv\Scripts\python.exe -m pytest tests\step_01_technical_test.py -v`，实测 `28 passed, 1 skipped`（跳过的是真实服务连通性探测）；设置真实服务开关后运行完整版本：

```powershell
$env:EASYOFFICE_REAL_SERVICES="1"
.venv\Scripts\python.exe -m pytest tests\step_01_technical_test.py -v
```

此时 29 项全部通过，其中连通性测试直接读取项目根 `config.yaml` 并对真实 Embedding、VLM 与本地解析权重执行探测，不再使用进程内假 HTTP 服务。

除脚本外，人工还可直接检查真实运行产物：用任意 SQLite 客户端打开临时运行目录或项目根 `runtime/knowledge.db`，查看 `files`、`tags`、`file_tags`、`chunks` 与 `chunk_fts` 表；到 `runtime/mirror/<uid前两位>/<uid>.txt` 查看镜像行；到 `managed_src_files/` 查看托管上传文件；到 `runtime/assets/` 查看从真实文档裁剪出的图片。上述检查与脚本断言一致。

### 3）对开发计划和前序步骤已完成代码的调整（如有）

本步骤开始时 `docs/PROGRESS.md` 实际是一个 0 字节的小写文件 `docs/progress.md`，没有任何前序步骤成果，因此本次从步骤一开始，并将文件重命名为计划要求的 `PROGRESS.md`；重命名只涉及文件名，没有改动任何已有内容。

中文全文检索方案在计划中只写了“FTS5 配合中英文分词器”，但 Python 自带 SQLite 的 `unicode61` 分词器会把连续中文当成一个 token，直接使用会导致中文检索失效。经与用户确认，本实现改为先用 jieba 对文本和查询做一致性分词，再把分词结果以空格连接写入 FTS5；这样中英文都使用同一套词流，BM25 的词频统计有效。该方案对单个汉字的查询覆盖有限，因为 jieba 词流中不一定存在单字 token；计划中的 ripgrep 链路正是这类精确子串需求的补充，后续步骤无需为此改动本层接口。

“向量维度一致性校验”在计划中没有定义具体语义，本实现将其明确为三条规则：配置维度必须为正；已有向量时禁止直接修改维度，必须显式清空切片/向量后重建；每次写入切片向量与查询向量时逐条校验维度。这样既能在配置更新阶段拦截错误，也能在索引写入阶段拦截调用方错误。

“宿主机原地引用模式的路径白名单”在计划中没有定义默认行为。经与用户确认，空白名单表示不限制路径，适合本机单用户首发；一旦配置白名单，登记、复制、移动的目标与来源都必须位于白名单根目录内，越界会抛出 `PathNotAllowedError`。所有路径都会展开环境变量、转绝对路径并解析符号链接，避免通过 `..` 或链接绕过白名单。

三类模态专属摘要“互不冗余”的要求被实现为数据库层的单列约束：文件只能有一个 `modality_summary_kind`，已有办公摘要的文件再写图像摘要会抛出 `ModalitySummaryConflict`，同类型摘要可以反复覆盖更新。这个约束比在应用层做判断更可靠，后续步骤不会因为并发或代码路径不同而产生多类摘要并存。

配置路径与数据目录的默认语义在步骤二后期做了目录拆分，当前语义为：配置文件默认是项目根 `config.yaml`，可用 `EASYOFFICE_CONFIG` 覆盖；运行产物根目录默认是项目下 `runtime/`，可用 `EASYOFFICE_RUNTIME`（兼容旧变量 `EASYOFFICE_HOME`）覆盖；`storage.index_db_path`、`mirror_cache_dir`、`asset_cache_dir`、`temp_dir` 相对 `runtime/` 解析，`managed_workspace`、`local_parser.weights_dir`、`host_watch_dirs`、`host_allowed_roots` 相对配置文件所在目录解析。`managed_workspace` 默认 `managed_src_files/`，`weights_dir` 默认 `models/`。运行时修改数据库路径会打开新数据库但不迁移旧数据，修改托管工作区或镜像目录也不会搬移已有文件；这是轻量首发的有意取舍，后续如需迁移应单独实现迁移工具（步骤二已提供 `scripts/migrate_layout.py`）。

本实现只支持 `sqlite` 向量/全文索引驱动与 `local` 对象存储驱动，配置中出现其他驱动会被核心启动或配置热更新拒绝；但 `HybridIndexStorage` 与 `FileStorage` 两个抽象接口已经按计划预留了 Elasticsearch、MinIO 等扩展点，未来只需新增适配器并放宽驱动校验，不需要改动上层调用。

配置中心的“接口连通性探测”被实现为无副作用的探测：OpenAI 兼容服务请求 `<base_url>/models`，Ollama 请求 `<base_url>/api/tags`，本地模式检查权重目录中是否存在实际文件（仅存在空目录不算配置完成），本地解析权重探测同时返回设备与 OCR 开关信息。探测失败只返回 `unreachable`/`not_configured`/`disabled` 等结构化结果，不会抛出网络异常，也不会修改配置。

在核心装配层还补充了几项计划未明确但影响正确性的行为：同一路径重新登记且内容哈希变化时，`EasyOfficeCore.register_inplace` 会主动清理该文件已有的切片和伴生镜像，防止检索在重新解析前返回陈旧内容；切片的 `plain_text` 列强制保存剥标签后的纯文本，`structured_text` 保留原始 HTML，避免 HTML 标签污染 FTS 与镜像；镜像关闭时会删除对应文件的旧镜像；数据库增加了扩展名+修改时间、扩展名+大小、存储模式+修改时间、扩展名+业务时间等复合索引，以贴合计划中的复合筛选场景；本地解析权重探测改为检查目录内容而不是目录存在性。

另外需要说明的是，步骤一自身不包含文档解析、OCR、视觉理解、语义提炼或嵌入模型调用，这些能力分别由步骤二至五实现。步骤一对外提供的是存储、元数据、混合索引和伴生镜像契约；步骤二接入真实解析与真实模型后，`tests/step_01_business_walkthrough.py` 已经改为直接复用 `core.office` 的真实解析结果，并用真实 Embedding/VLM 完成入库、检索和三类模态摘要回填，不再由测试脚本人工构造切片或向量。底层单元测试仍使用确定性的单位向量验证 SQL、余弦相似度和维度校验等数学行为，这些数值只是索引层的测试输入，不代表任何模型输出。

### 4）给后续步骤的开发说明

本步骤对外暴露的主要入口是 `EasyOfficeCore`（`easy_office/core.py`）。后续步骤应优先通过它获取组件，而不是各自重复连接数据库：`EasyOfficeCore.bootstrap(config_path=None, data_dir=None, config_patch=None)` 负责创建配置中心、目录、数据库和全部服务；实例属性 `config`、`storage`、`db`、`metadata`、`index`、`mirror` 分别暴露各层；便捷方法包括 `register_inplace(path)`、`ingest_upload(file_name, stream, fs_created_at=None, fs_modified_at=None, relative_path=None)`、`sync_moved_path(file_uid, new_path)`、`move_file(file_uid, destination)`、`delete_file(file_uid, delete_managed_bytes=True)`、`add_chunks(file_uid, chunks)`、`search_bm25(query, file_filter=None, top_k=10, operator="OR")`、`search_vector(query_vector, file_filter=None, top_k=10)` 和 `close()`。`EasyOfficeCore` 支持上下文管理器。

配置中心的公开接口为 `get()`、`public_config()`、`raw_config()`、`update(patch, persist=True)`、`set(path, value)`、`replace(config)`、`reload()`、`subscribe(listener)`、`add_validator(validator)`、`paths()`、`ensure_directories()`、`resolve_path(value)` 与 `test_connectivity(target="all")`。`public_config()` 是唯一适合暴露给客户端的形式；`raw_config()` 包含真实密钥，不应出现在网络接口或日志中。`paths()` 返回解析后的 `ResolvedPaths`，后续守护进程和 CLI 可以直接用它定位数据目录、配置、数据库、托管区、镜像区和权重目录。配置监听器接收新的 `AppConfig` 深拷贝；自定义校验器接收 `(old, new)` 并可抛异常阻止更新。

存储层的接口是 `FileStorage` 抽象（`storage/base.py`）与默认实现 `LocalFileStorage`（`storage/local.py`）。URI 当前格式为 `local:<绝对路径>`，`StoredObject` 同时携带 `local_path` 与 `uri`，后续归档工具移动文件时应使用 `core.move_file` 或 `storage.move`，移动后调用 `core.sync_moved_path` 或 `metadata.update_path`，以保持数据库路径与磁盘一致。宿主机原地模式的登记不会复制字节；跨端托管模式的文件位于 `storage.managed_workspace`。对于托管上传，`parent_dir` 是实际托管目录，`relative_path` 记录客户端提供的原始相对路径；对于原地文件，只有配置了白名单根目录时 `relative_path` 才有值。未来接入 MinIO 时实现同一 ABC，并把 `core` 中的存储装配替换成新驱动即可。

元数据中心的接口集中在 `MetadataStore`。`register_file(obj)` 对同一 `(storage_mode, local_path)` 做更新而不是重复插入；`update_semantic_metadata(file_uid, topic_tags=..., summary=..., entities=..., keywords=..., business_start_date=..., business_end_date=...)` 支持部分更新，未传入的字段保持不变，传入 `None`/空列表表示清空；`update_modality_summary(file_uid, kind, summary, extra_tags=None)` 中的 `kind` 只能是 `office`/`image`/`table`，`extra_tags` 只接受 `visual`/`column` 两个命名空间；`clear_modality_summary(file_uid)` 用于重新挂接另一种模态摘要。复合查询使用 `FileQuery`，支持扩展名、MIME、存储模式、名称子串、父目录前缀、大小区间、修改时间区间、主题标签（任意/全部）、关键词、实体、业务时间重叠、模态类型和任意模态 JSON 字段过滤。`build_filter_sql(query, alias="fi")` 返回条件与参数，供步骤六的四路检索在 `files` 表联查中复用，调用方不应自行拼接过滤 SQL。

索引层的接口是 `HybridIndexStorage`（`indexing/base.py`）与默认实现 `SQLiteHybridIndexStore`（`indexing/sqlite_store.py`）。步骤二至四构造切片时应填写 `ChunkInput` 的 `chunk_order`、`plain_text`、`structured_text`、`chunk_type`、页码/工作表/行列、`bbox`、`image_path` 和可选的 `embedding`；`chunk_order` 在同一文件内唯一，同一序号再次写入会替换旧切片并同步更新 FTS、向量和镜像。`search_bm25` 返回按 BM25 排序的 `SearchHit`，`search_vector` 返回按归一化余弦相似度排序的 `SearchHit`，两者都携带完整的 `ChunkRecord` 与 `FileRecord`，后续步骤可直接从中取源文件路径、页码、坐标框、结构化原文和命中链路。`get_chunks(file_uid, page_start=..., page_end=...)` 为步骤七的“定向翻页精读”提供了连续页面读取能力。

伴生镜像的接口是 `MirrorCache`（`indexing/mirror.py`）。`sync_chunks(file_uid, chunks)` 由索引层在切片变化时自动调用，后续解析管道通常不需要手动写镜像；若某个步骤需要写入非切片来源的镜像文本，可以使用 `MirrorLine` 与 `write_lines`。ripgrep 链路命中镜像行后，用 `MirrorLine.parse(line)` 解析前缀，再调用 `mirror.describe_hit(line, metadata.get_file)` 得到源文件路径、页码、工作表、行列范围与原文；镜像行格式是 `@@EO|<uid>|<kind>|<page_start>|<page_end>|<sheet>|<row_start>|<row_end>|<col_start>|<col_end>|<text>`，工作表名经过百分号编码，正文在最后一个字段。

数据库层的 `Database` 暴露 `transaction()`、`execute()`、`fetchone()`、`fetchall()`、`embedding_dim`、`vector_count()`、`set_embedding_dim()`、`reopen()` 等能力。后续步骤如果直接使用数据库，应遵守“写入走 `transaction()`、读取走 `fetchone/fetchall`”的约定；步骤五的多进程索引必须按计划把各工作进程的解析结果回传主进程，由主进程用单连接批量写入，避免多进程并发写 SQLite。当前 `Database` 内部有可重入锁，适合守护进程的线程池，但不适合多个进程各自持有连接写入。

已知遗留问题与注意事项如下。第一，步骤一自身不实现文档解析、OCR、VLM 调用、语义提炼和嵌入模型；其业务演示已改为复用步骤二的真实解析流水线与真实模型服务，底层单元测试中的单位向量只用于验证索引数学契约，不代表模型输出。第二，修改 `embedding_dim` 时如果已有向量会被拒绝；需要先删除或重建切片，再修改配置。第三，修改数据库路径会打开一个新的空数据库，不会迁移旧数据；修改托管区或镜像目录也不会搬移已有文件，这是首发的轻量取舍。第四，敏感密钥以明文保存在本地 YAML 中，仅在 `public_config()`/网络导出中脱敏，部署时应限制配置文件权限。第五，中文 BM25 依赖 jieba 词流，单字查询和未登录词可能无法命中，复杂正则与精确子串应由步骤六的 ripgrep 链路承担。第六，镜像文件目前是按整文件重写，超大文档的增量镜像写入性能需要在后续步骤评估。第七，标签匹配在 SQLite 中区分大小写，英文标签如需大小写不敏感匹配，应在后续检索层统一规范化。第八，`FileQuery.modified_after/modified_before` 接受 ISO-8601 字符串、`date` 或 `datetime`，内部统一转换为 UTC 时间戳字符串；其他无法解析的时间字符串会抛出 `MetadataValidationError`。第九，Windows 上使用 `st_ctime` 作为创建时间，Linux 上优先使用 `st_birthtime`，若文件系统不提供出生时间则回退到 `st_ctime`，两种平台的“创建时间”语义可能略有差异。第十，本步骤的连通性探测只验证服务可达性或权重文件存在性，不验证模型名称是否真的可用，真正的模型调用与错误处理留给后续在线步骤。

## 步骤二：多源版面办公文档统一解析框架、内嵌图表表格还原与结构化切片管道

### 1）开发内容解读（总分结构）

本步骤在步骤一的底座上新增了 `easy_office/parsing` 与 `easy_office/models` 两个包，并把它们接入 `EasyOfficeCore`。离线链路现在完整贯通为：登记文件、选择解析后端、旧格式定版转换、PDF 逐页数字/扫描分流、版面对象提取、图片过滤与 OCR/VLM 转写、按章节和页码切片、向量化、写入混合索引与伴生镜像、回填“办公文档专属结构摘要”。后续的步骤三、四、五只需要复用这条管道的输出契约，不再关心 Docling 的具体对象模型。

**可插拔解析框架与默认 Docling 驱动**由 `parsing/base.py`、`parsing/models.py` 与 `parsing/docling_parser.py` 实现。`DocumentParser` 抽象只要求驱动把任意格式转换成统一的 `ParsedDocument`，其中包含按阅读顺序排列的 `ParsedBlock`、带跨度和表头标记的 `ParsedTable`、带原图字节和页坐标的 `ParsedPicture`、`ParsedFormula` 以及逐页 `PageProbe`。`ParserRegistry` 以名称注册工厂，当前只注册 `docling`，未来接入 MinerU 或 DeepSeek-OCR2 时新增一个工厂即可，切片、摘要和索引层无需改动。Docling 采用惰性导入，未安装时核心服务仍可启动；权重目录通过 `DOCLING_ARTIFACTS_PATH` 指向步骤一配置的 `local_parser.weights_dir`。

**新旧两代办公格式的定版兼容**由 `parsing/libreoffice.py` 实现。DOCX 与 PPTX 直接交给 Docling 原生后端；DOC 与 PPT 先通过无头 LibreOffice 转换成原生数字版 PDF，再进入同一套 PDF 解析通道。转换器会依次从配置项、`EASYOFFICE_SOFFICE` 环境变量、`PATH` 和常见安装目录查找 `soffice`，每次转换使用独立的临时用户配置目录，支持同步与异步两种调用方式，并在遇到 LibreOffice 首次启动崩溃时自动重试一次。转换完成后所有切片与摘要都映射回原始 DOC/PPT 文件，摘要中记录 `rendered_via: libreoffice_pdf`。

**PDF 数字页与扫描页的智能分流**由 `parsing/pdf_probe.py` 与 `docling_parser.py` 配合完成。探针逐页统计原生字符数、乱码比例和内嵌光栅图覆盖率，给出 `is_scanned` 与 `formula_hint`。全数字文档走关闭 OCR 的高速矢量通道；全扫描文档走开启全页 OCR、版面分析、表格结构重建和公式富化的视觉通道；两者混排时把页码拆成两个子集分别转换，再按原页码映射合并，保证页码与阅读顺序不乱。数字页还会用原生文本层做确定性的公式征兆判断，避免为了一个布尔标志在 CPU 上跑重型公式模型。

**办公文档专属结构摘要**由 `parsing/summary.py` 计算并回填。总页数、总字符数、主要语种、载体类型（`digital`/`scanned`/`mixed`）、是否含表格/公式/图表图片等标志全部来自解析对象和页探针的确定性统计；公式标志区分 `parser`（Docling 识别出公式对象）、`native_text`（PyMuPDF 原生文本层出现公式征兆）和 `heuristic`（正文或 OCR 文本出现公式模式）三种来源。摘要通过步骤一的 `update_modality_summary(file_uid, "office", ...)` 写入元数据主表。

**表格的零 Token 双格式转换**由 `parsing/tables.py` 实现。它直接在内存中检查四个确定性条件：存在 `row_span > 1` 或 `col_span > 1`、表头行数不少于两行、单元格内含换行或列表标记、各行有效跨列宽度不一致。命中任意一条就输出只含 `<table>`、`<thead>`、`<tbody>`、`<tr>`、`<th>`、`<td rowspan colspan>` 的精简 HTML，合并单元格只在起点渲染一次；四条都不满足才输出 Markdown。整个过程不调用模型、不消耗 Token。无论哪种格式，都会另外生成一份去标签的表头加行文本，供 FTS、Embedding 和伴生镜像使用。

**内嵌图片的四步转写**由 `parsing/images.py` 实现。第一步把 Docling 裁剪出的图片写入步骤一新增的资产缓存目录；第二步做确定性过滤，剔除边长或面积过小、长宽比失衡、跨页固定出现在页眉/页脚位置的装饰图，以及覆盖整页的扫描页图像；第三步对保留下来的实质性图片运行 RapidOCR，并调用步骤一配置的 VLM 生成场景类别、背景、主体、视觉标签与自然语言描述，未配置 VLM 或调用失败时退化为“尺寸 + 图注 + OCR 文字”的可检索描述；第四步把图注、前后相邻正文、视觉描述和 OCR 文字组装成独立切片，并绑定裁剪图路径。图片切片与文本、表格、公式切片一起写入混合索引，伴生镜像由索引层按页码自动同步。

**章节树切片与索引流水线**由 `parsing/chunker.py` 与 `parsing/pipeline.py` 实现。切片器维护标题层级栈，正文按“同一页面、同一章节、不超过 `max_chunk_chars`”聚合，每个切片注入 `[章节: A > B | 第 N 页]` 前缀；表格、图片、公式各自成为独立切片；PPTX 使用“第 N 张幻灯片”，DOCX 使用流式页语义而不写页码前缀。流水线负责解析、图片处理、摘要回填、Embedding 批量计算、`replace_file_chunks` 写入以及失败清理。解析器实例按配置缓存复用，避免每个文件重复加载数 GB 的模型；遇到旧格式文件时先释放解析器再调用 LibreOffice，转换完成后重新创建。

**真实模型服务适配**由 `models/services.py` 与 `models/ocr.py` 实现。Embedding 与 VLM 都走 OpenAI 兼容接口，支持 `openai_compatible` 与 `ollama` 两种服务商；HTTP 请求使用标准库实现，带总时限和错误分类。Embedding 客户端逐条校验向量维度，并在服务端返回“batch size 过大”时自动二分重试。VLM 客户端输出固定 JSON 结构，解析失败或超时都会回退到 OCR 降级描述。RapidOCR 同时兼容新旧两个 Python 包，关闭其控制台噪声日志。

#### 核心代码批注

Docling 的权重目录有一个容易踩的坑：如果 `DOCLING_ARTIFACTS_PATH` 指向一个空目录，Docling 会在构造流水线时直接报错，连 DOCX/PPTX 这种不需要版面模型的格式也无法解析。解析器因此只在目录已经填充模型时才设置环境变量，否则主动清掉自己设置的空路径：

```python
if populated:
    os.environ.setdefault("DOCLING_ARTIFACTS_PATH", str(self._weights_dir))
elif os.environ.get("DOCLING_ARTIFACTS_PATH") == str(self._weights_dir):
    os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
```

PDF 混合页分流的合并逻辑是另一个关键点。数字页子集和扫描页子集各自从第 1 页开始编号，因此不能用子集页码直接写库；解析子集时建立“子集页号 → 原文件页号”的映射，合并后再按原页码和阅读顺序重新编号：

```python
mapping = {index + 1: page_no for index, page_no in enumerate(pages)}
parsed_parts.append(self._extract(..., page_mapping=mapping))
...
blocks.sort(key=lambda block: (block.page_no or 0, block.order))
```

公式富化是 CPU 上最重的环节之一，计划中明确要求扫描页做 LaTeX 还原，而数字页已有原生字符流。因此本实现把 `do_formula_enrichment` 只打开在强制全页 OCR 的扫描路由上；数字页改用 PyMuPDF 原生文本层的公式征兆判断补齐 `has_formulas`：

```python
self._set(
    options,
    "do_formula_enrichment",
    bool(self._config.formula_enrichment) and bool(force_full_page_ocr),
)
```

表格 HTML 序列化时，合并单元格在 `TableData.table_cells` 中只出现一次，但覆盖多个网格位置。渲染器先建立覆盖矩阵，再只在单元格的起点输出标签，这样既不会重复输出文本，也能让 `rowspan`/`colspan` 正确表达统属关系：

```python
if cell.row_start != row or cell.col_start != col:
    continue
tag = "th" if cell.is_header else "td"
if cell.row_span > 1:
    attrs.append(f'rowspan="{cell.row_span}"')
```

图片过滤除了尺寸和长宽比，还处理两类“看起来很大但不应入库”的图：跨页固定出现在页眉/页脚区域的 Logo，以及覆盖整页的扫描页图像。后者在扫描件里非常常见，如果不剔除，会对整页重复做一次独立 OCR 和 VLM 调用：

```python
if picture.page_no in scanned_pages:
    if coverage >= self._config.scanned_page_image_coverage:
        return "page_image"
```

切片器不在正文里重复标题文本：标题只更新面包屑栈，后续正文切片的前缀自然携带完整章节路径；这避免了“只有一行标题”的碎片切片，也让 BM25 和向量都能看到章节语义：

```python
heading_stack = heading_stack[: level - 1] + [block.text.strip()]
...
head = self._prefix(heading_stack, page_no, document)
```

Windows 上 LibreOffice 无头转换在父进程已经加载 Docling 的 PyTorch 模型时偶发 `0xC0000409` 崩溃；临时用户配置目录放在很深的 `pytest`/托管工作区路径下也会让 LibreOffice 触到路径长度限制。处理方式是转换前释放解析器、把 profile 放到系统临时目录、失败后重试并接受“返回码异常但 PDF 已生成”的情况：

```python
self._release_parser()
converted = self._convert_legacy(source, workspace, config)
parser = self._get_parser(config, paths)
```

DashScope 的 Embedding 接口单批最多 20 条，而通用配置默认允许 32 条。客户端在收到明确的批量过大错误时自动二分，不要求每个部署都预先知道服务商限制：

```python
if len(batch) > 1 and "batch size" in message and ("larger than" in message or "invalid" in message):
    midpoint = len(batch) // 2
    return self._embed_batch(batch[:midpoint]) + self._embed_batch(batch[midpoint:])
```

### 2）完整的测试方法

测试环境为 Windows 11、Python 3.11.9、Docling 2.140.0、PyMuPDF、RapidOCR、LibreOffice，以及用户提供的真实 OpenAI 兼容服务：Embedding 为 `qwen3.7-text-embedding`（1024 维），VLM 为 `deepseek-flash`。Docling 权重缓存约 1.3 GB，位于项目根 `models/`；真实测试文档位于 `data/`，包括数字版 PDF、扫描版 PDF、DOCX、PPTX、DOC 与 PPT。测试代码只使用真实 Docling 解析器、真实 Embedding/VLM/RapidOCR 客户端与真实文档；所有涉及模型与文档解析的验收都使用真实服务与真实文件。表格复杂度、切片边界、摘要统计等纯逻辑单元测试使用内存构造的对象，只验证确定性算法，不代表模型输出。

第一档是面向人类审查的产品业务链路，脚本为 `tests/step_02_business_walkthrough.py`。它从产品使用者视角串起一条完整链路：读取项目根 `config.yaml` 中的真实配置，确认真实 Embedding 与 VLM 已配置，扫描真实文档目录并逐个登记文件；对每个办公文档调用 `core.office.ingest_path`，由 `OfficeDocumentPipeline` 完成“选择解析后端 → 读取 DOCX/PPTX 原生结构或把 DOC/PPT 经 LibreOffice 定版为 PDF → PDF 逐页数字/扫描分流 → Docling 版面、表格、图片与公式提取 → 装饰图过滤 → RapidOCR 与 VLM 图片转写 → 章节/页码/坐标框切片 → 真实 Embedding 批量向量化 → 写入 FTS5/sqlite-vec 混合索引与伴生镜像 → 回填办公结构摘要”；随后在终端逐份打印格式、载体类型、页数、切片数、表格数、保留/过滤图片数、公式数、向量数和耗时，展示结构摘要 JSON、复杂表格 HTML、真实 VLM 图片描述、伴生镜像前三行，以及 BM25 与真实向量检索结果。退出码 0 表示全部文档入库成功，非 0 表示存在失败并在末尾列出原因。

脚本参数与设置方法如下。第一个位置参数 `docs` 是真实文档目录，默认项目下的 `data`；脚本只扫描该目录中的 `.pdf`、`.docx`、`.pptx`、`.doc`、`.ppt`，并跳过 `~$` 开头的 Office 锁文件以及运行目录下的派生产物，避免把临时 PDF 或裁剪图当成源文档。`--data-dir PATH` 指定可删除重建的运行目录，默认项目根 `runtime/`；脚本在不存在真实 Embedding/VLM 配置时直接退出，防止在未接模型的环境里产生半成品索引。`--config PATH` 指定真实配置文件，默认项目根 `config.yaml`。`--query TEXT` 指定 BM25 和向量检索的查询；缺省时脚本会从第一份文档的第一个有效正文切片中自动截取一段文本作为查询。`--limit N` 只处理排序后的前 N 个文件，便于先小范围验证。脚本没有关闭模型的开关，公式富化、OCR、Embedding、VLM 的行为完全由 `config.yaml` 中的 `local_parser.formula_enrichment`、`local_parser.ocr_enabled`、`embedding` 和 `vlm` 决定。正式运行命令为：

```powershell
.venv\Scripts\python.exe tests\step_02_business_walkthrough.py data --query 功率预测
```

这条链路依次调用的组件与接口是：`EasyOfficeCore` 与 `OfficeDocumentPipeline.ingest_path/ingest` 负责编排；`LocalFileStorage.register_inplace` 负责零拷贝登记；`DoclingParser.parse` 负责版面对象提取；`LibreOfficeConverter.convert_to_pdf` 负责 DOC/PPT 定版；`probe_pdf/digital_and_scanned_pages/split_pdf` 负责 PDF 逐页分流与子集合并；`ImageProcessor.process` 负责装饰图过滤、裁剪图落盘、RapidOCR 与 VLM 转写；`OfficeChunker.build` 负责章节树切片；`build_office_summary` 负责结构摘要；`EmbeddingClient.embed_texts` 负责真实批量向量化；`SQLiteHybridIndexStore.replace_file_chunks/search_bm25/search_vector` 负责混合索引与检索；`MirrorCache.sync_chunks/read_lines` 负责伴生镜像；`MetadataStore.update_modality_summary` 负责办公摘要回填。人类审查时只需要看每个文件的汇总行、结构摘要、HTML 表格、图片描述和检索结果，不需要阅读解析器内部代码。

最终真实业务数据为 6 份文档、122 个切片与 122 个向量、6 份伴生镜像：扫描版标准 PDF 3 页/10 切片/2 表格/1 张整页扫描图被过滤/10 向量（公式标志由 OCR 文本启发式判真）；PPT 5 页/20 切片/7 张有效图/11 张装饰图被过滤/20 向量；DOC 6 页/26 切片/2 表格/3 图/26 向量；DOCX 流式页/29 切片/1 表格/3 图/41 张装饰图被过滤/29 向量；数字版 PDF 5 页/23 切片/1 表格/3 图/23 向量，原生文本层在 2 页上识别出公式征兆；PPTX 4 张幻灯片/14 切片/4 图/14 向量。伴生镜像行数分别为 106、85、254、237、247、86。BM25 对“功率预测”正确召回扫描件表格、标准条款和论文正文；真实 1024 维 Embedding 向量检索召回 PPTX 与数字版 PDF 的相关页面；真实 VLM 对 20 张保留图片全部返回视觉标签与描述。需要说明的是，扫描版 PDF 的公式富化模型在 CPU 上耗时较长，最终业务库的扫描件结果使用了关闭公式富化的配置；公式开启路径已由扫描件技术测试单独验证，可产出 2 条 LaTeX 公式。

第二档是面向开发者的底层测试，脚本为 `tests/step_02_technical_test.py`。它覆盖表格复杂度四条规则的独立触发、HTML 精简标签与转义、Markdown 简单表、合并单元格行列跨度、多级表头、单元格换行、正文公式启发式、原生文本层公式征兆、载体类型判定、办公摘要字段、章节面包屑与页码前缀、跨页切片边界、表格/图片/公式独立切片、图片尺寸与长宽比/跨页页眉/整页扫描图过滤、未配置 VLM 时的 OCR 降级、真实 PDF 探针与拆分、真实 DOCX/PPTX 的 Docling 原生解析、解析器注册表、文件类型拒绝、资产目录配置、真实服务的 Embedding 维度与批量上限拆分、真实服务连通性，以及真实文档全量入库。纯逻辑单元测试使用内存对象，不调用任何模型；真实服务测试在设置 `EASYOFFICE_REAL_SERVICES=1` 后执行，真实文档全量测试在设置 `EASYOFFICE_STEP2_DOCS=data` 后执行。完整命令为：

```powershell
# 默认：纯逻辑 + 真实文档上的 Docling/PDF 探针（不访问外部模型）
.venv\Scripts\python.exe -m pytest tests -q

# 真实 Embedding/VLM/连通性
$env:EASYOFFICE_REAL_SERVICES="1"
.venv\Scripts\python.exe -m pytest tests -q -k "real_"

# 真实文档全量解析、OCR、定版转换与摘要回填
$env:EASYOFFICE_STEP2_DOCS="D:\code\easy-office\data"
.venv\Scripts\python.exe -m pytest tests\step_02_technical_test.py::test_real_documents_ingest -v
```

默认运行结果为 `55 passed, 5 skipped`；5 个跳过项中 1 个是步骤一的真实连通性测试、3 个是步骤二的真实模型服务测试、1 个是需要 `EASYOFFICE_STEP2_DOCS` 的真实文档全量测试。设置 `EASYOFFICE_REAL_SERVICES=1` 后，`-k "real_"` 会执行 4 个真实模型服务测试和 2 个真实文档解析测试，实测 `6 passed, 1 skipped`（跳过的是仍需 `EASYOFFICE_STEP2_DOCS` 的全量测试），其中 4 个模型服务测试约 7 秒完成；设置 `EASYOFFICE_STEP2_DOCS=data` 后，真实文档全量测试 `1 passed`，实测约 144 秒，逐份解析 PDF、DOCX、PPTX、DOC、PPT，断言切片数与页数大于零、办公摘要字段齐全、模态摘要类型为 `office`、伴生镜像存在。该测试为避免 CPU 公式长尾，在临时测试配置中关闭了 `formula_enrichment`；公式开启路径已由扫描件技术测试和真实业务库单独验证。人工检查可以直接打开项目根 `runtime/knowledge.db` 的 `files`、`chunks`、`chunk_fts` 表，或查看 `runtime/assets/<uid前缀>/<uid>/picture-*.png` 与 `runtime/mirror/<uid前缀>/<uid>.txt`。

### 3）对开发计划和前序步骤已完成代码的调整（如有）

第一项调整是 DOCX 的页码语义。DOCX 格式本身没有物理分页信息，经用户确认采用“流式页语义”：DOCX 内容仍由 Docling 原生解析，所有切片页码记为 1，办公摘要的 `total_pages` 记为 1，切片不写页码前缀，也没有可靠的页面矩形坐标。PPTX 使用幻灯片序号，PDF 与由 DOC/PPT 定版生成的 PDF 使用物理页码。这个取舍写入了 `page_semantics` 字段，后续检索依据展示时可以区分 `flow`、`slide` 与 `physical`。

第二项调整是公式富化的启用范围。计划把公式 LaTeX 还原放在扫描页视觉通道，但实现中最初对所有 PDF 开启，实测在 CPU 上对公式较多的文档可能长时间不返回。现在 `do_formula_enrichment` 只在扫描页路由开启；数字页通过 `parsing/text_signals.py` 对 PyMuPDF 原生文本层做公式征兆判断，摘要中记录 `formula_detection`。扫描件在开启公式富化时实测可产出 LaTeX 公式对象，关闭时仍可通过 OCR 文本的公式模式把 `has_formulas` 标为真。

第三项调整是配置结构。为承载资产裁剪图和解析参数，`StorageConfig` 新增 `asset_cache_dir`（默认 `assets`），`LocalParserConfig` 新增 `formula_enrichment`、`libreoffice_path`、`scanned_page_dpi`、`digital_text_min_chars`、`scan_image_area_ratio`、图片最小边长/面积/最大长宽比、整页图片覆盖阈值、`max_chunk_chars`。核心启动校验现在明确只接受 `storage.parsing_backend='docling'` 与既有的 `sqlite`/`local` 驱动；解析器注册表本身仍为 MinerU、DeepSeek-OCR2 等未来后端保留扩展点。

第四项调整是 LibreOffice 的稳定性处理。Windows 下父进程加载 Docling 模型后再启动 LibreOffice，偶发 `0xC0000409` 崩溃；把用户配置目录放在较深的托管工作区路径下也会触发路径长度问题。实现改为转换前释放解析器、profile 使用短系统临时路径、失败自动重试，并在返回码异常但目标 PDF 已生成时接受结果。异步版本同样支持。这一调整不改变对外接口。

第五项调整是 Embedding 批量策略。真实 DashScope 服务单批最多 20 条，而项目配置默认 32 条，业务演示首次运行时 3 个文档在向量化阶段被 400 拒绝。客户端现在识别“batch size 过大”错误并自动二分重试；同时给 HTTP 请求增加了总时限，避免服务端缓慢流式返回时无限等待。该问题也促使我们把业务演练中 3 个失败文档单独补跑，最终 6 份文档全部完成向量化。

第六项调整是图片过滤与启发式增强。新增“整页扫描图”过滤，避免对扫描页自带的整页位图重复做 OCR 和 VLM；新增 PPTX 幻灯片标题提升规则，Docling 把标题当普通文本输出时，取每页第一段长度不少于 4 个字符的文本作为章节标题；新增 DOCX/PPTX 图注启发式，Docling 未建立图注关联时从图片前文末尾识别“图N/Figure N/表N”片段。

第七项是切片与脚本的小幅修正。标题不再进入正文缓冲区，避免产生只有标题的碎片切片；业务脚本会跳过 `runtime/`、`managed_src_files/`、`models/` 等项目级运行/缓存目录，避免把临时转换出的 PDF 当成源文件；`EasyOfficeCore.bootstrap` 新增 `persist_patch` 参数，允许测试或临时运行覆盖配置而不写回 YAML；`.gitignore` 增加 `.env`，避免真实密钥被误提交。

第八项调整是目录布局拆分。步骤一最初把配置、索引、镜像、资产、托管文件和模型权重都放在 `data/` 下，这对自包含演示方便，但把用户配置、可重建的派生状态、大模型缓存和源数据混在了一起。现在改为：项目根 `config.yaml` 放用户配置与密钥；`runtime/` 放可删除重建的派生状态（`knowledge.db`、`mirror/`、`assets/`、`tmp/`）；`models/` 放 Docling 权重缓存；`managed_src_files/` 放跨端上传的托管源文件（持久、不可当缓存删）；`data/` 只放可选的 inplace 源文档。`ConfigManager` 新增 `EASYOFFICE_CONFIG` 覆盖配置路径、默认运行目录改为 `runtime/`（`EASYOFFICE_RUNTIME`，兼容 `EASYOFFICE_HOME`），并把路径解析拆成 `resolve_runtime_path`（index/mirror/assets/temp）与 `resolve_project_path`（managed_workspace/weights_dir/host_watch_dirs/host_allowed_roots）；`ResolvedPaths` 增加 `temp_dir`，`AssetCache` 的临时工作区改放 `runtime/tmp/`；`scripts/migrate_layout.py` 完成现有文件迁移并同步更新数据库中已存的图片绝对路径。README、测试脚本和两份总结中的路径说明已同步到新布局。

### 4）给后续步骤的开发说明

本步骤对外最重要的入口仍是 `EasyOfficeCore`。新增的 `core.office` 属性返回 `OfficeDocumentPipeline`，支持 `ingest(file_uid)`、`ingest_async(file_uid)`、`ingest_path(path)`、`ingest_path_async(path)` 与 `supports(path_or_extension)`。`OfficeIngestResult` 暴露文件 UID、格式、解析后端、页数、切片数、向量数、表格/图片/公式计数、载体类型、页语义、耗时、摘要和图片处理统计。后续统一索引入口应优先使用 `core.office.ingest_path` 或 `ingest`，不要自行连接 Docling 或重复实现旧格式转换。

解析层的稳定契约在 `easy_office/parsing/models.py`。`ParsedDocument` 包含 `blocks`、`pages`、`warnings`、`page_semantics`、`page_count`、`converted_pdf_path`；`ParsedBlock` 用 `kind` 区分 `heading`/`text`/`list`/`table`/`picture`/`formula`/`code`，特殊块通过 `table`、`picture`、`formula` 字段携带结构化对象；`ParsedTable` 的每个 `ParsedCell` 都带起止行、起止列、表头标记与合并跨度；`ParsedPicture` 携带原图字节、像素尺寸、页坐标、图注、前后上下文、OCR 文本、VLM 描述与资产路径。步骤三、四可以直接复用 `ParsedPicture` 与表格对象，不必重新解析文档。

`ParserRegistry`、`DocumentParser` 与 `default_registry()` 是未来接入其他解析后端的扩展点；新增后端只需实现 `parse()` 并返回 `ParsedDocument`，然后在注册表中注册名称，同时放宽 `EasyOfficeCore._assert_supported` 中目前只允许 `docling` 的校验。`DoclingParser.close()` 可释放已加载的转换器和模型引用，适合在长时间运行的进程中按需调用。

图片与模型能力方面，`AssetCache` 提供按文件 UID 分目录的图片写入、临时工作区和清理；`build_embedding_client`、`build_vision_client`、`build_ocr_engine` 分别返回已配置的客户端或 `None`，调用方应把 `None` 视为“未配置，走降级逻辑”。`EmbeddingClient.embed_texts` 负责批量、维度校验与批量过大拆分；`VisionClient.describe_image` 返回结构化 `VisionDescription`，`ask_about_image` 可做自由视觉核验；RapidOCR 适配器同时兼容 `rapidocr` 与 `rapidocr_onnxruntime`。

表格工具 `analyze_table`、`render_table`、`render_html`、`render_markdown`、`table_plain_text` 可供步骤四的 CSV/Excel 小表复用，以保持 HTML/Markdown 双格式策略一致。摘要工具 `build_office_summary`、`carrier_type`、`detect_language` 与 `parsing/text_signals.looks_like_formula` 可以在后续步骤做统计和检索初筛时复用。

目录与路径方面，后续步骤应统一通过 `core.config.paths()` 获取配置路径、托管源文件目录、索引库、镜像、资产、临时目录与模型权重目录，不要硬编码 `data/`、`runtime/` 或 `models/`。`ResolvedPaths` 现在包含 `temp_dir`，并区分 `resolve_runtime_path` 与 `resolve_project_path`；步骤三/四/五新增的缓存或临时文件应放在 `runtime/` 下，新增的持久化源数据应放在项目根或 `managed_src_files/` 下，模型权重放 `models/`。

已知遗留问题与后续注意事项如下。第一，DOCX 没有物理页码和版面坐标，`page_semantics='flow'`，后续 Web UI 展示 DOCX 检索依据时应按段落/章节而不是页码定位。第二，数字版 PDF 不跑公式富化，因此 `formula_count` 可能为 0，公式存在性由原生文本征兆或 OCR 启发式标记；需要逐条 LaTeX 时应显式开启公式富化，并接受 CPU 耗时或改用 GPU。第三，公式富化在 CPU 上对公式密集文档可能非常慢，建议在线索引使用工作进程和单文件超时（步骤五），或通过 `local_parser.formula_enrichment=false` 关闭。第四，扫描件 OCR 质量受扫描分辨率、印章、水印和倾斜影响；真实标准 PDF 中出现了“稻壳阅读器/道客巴巴”水印被识别为正文的情况，后续可增加水印/页眉噪声过滤。第五，Docling 的 Word 后端对表头标记不完整，当前 HTML 仍能靠合并跨度正确表达结构，但 `<th>` 可能只覆盖第一行。第六，跨页表格尚未合并为单个表格对象，Docling 按页输出，步骤四/六如需跨页表格语义可再做后处理。第七，不同格式的坐标框单位不同：PDF 为页面点、PPTX 为 EMU、DOCX 无坐标；后续预览组件应结合 `PageProbe.width/height` 归一化。第八，VLM 调用有 30 秒总时限，超时或返回非 JSON 会退化为 OCR 描述，不会中断整份文档；Embedding 失败则会中止该文件的入库，确保不会写入“文本有、向量无”的半成品。第九，旧格式转换依赖 LibreOffice，配置项 `local_parser.libreoffice_path`、环境变量 `EASYOFFICE_SOFFICE` 或 PATH 任一路径可用即可；Linux 部署时安装对应发行版的无头 LibreOffice。第十，Docling 权重约 1.3 GB，放在项目根 `models/` 并被 `.gitignore` 排除，部署时需要重新下载；`scripts/prefetch_docling_models.py` 可在用户机器上完成预下载。第十一，解析器实例在单进程内缓存并被锁串行化，适合步骤二/五的工作进程模型，不适合在同一事件循环里高并发直接调用；步骤五应继续采用“进程解析、主进程批量写库”的架构。第十二，当前 `.docx` 测试文件中的公式未能被原生文本探针识别，`has_formulas=false`；这类公式如果以 OMML 或图片形式存在，后续可在步骤三的图像理解或步骤五的骨架提炼中补充。
