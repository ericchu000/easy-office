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

本步骤的测试环境为 Windows 11、Python 3.11.9、SQLite 3.45.1（已启用 FTS5 与扩展加载）、sqlite-vec 0.1.9、jieba 0.42.1、pytest 9.1.1。测试不依赖任何真实模型服务、外部数据或网络资源：所有文件都由脚本在临时目录生成，HTTP 连通性测试使用进程内的假服务，向量检索使用 8 维模拟向量。

第一档是面向人类审查的业务链路演示，脚本为 `tests/step_01_business_walkthrough.py`。它按用户使用产品的顺序串起一条完整故事：初始化配置与数据目录，在线修改 LLM 配置并观察脱敏与热重载，以宿主机原地引用模式登记一份“季度经营分析.docx”，以跨端上传托管模式接收一份 CSV 和一张 PNG，回填第二层语义元数据以及办公、表格、图像三类模态摘要，执行“主题 + 扩展名 + 表格标志 + 业务时间”的复合筛选，写入带页码和坐标框的切片并分别执行 BM25 与向量检索，查看伴生镜像的原始行，最后把经营分析文档移动到归档目录并用 SHA-256 同步数据库路径。运行命令为 `.venv\Scripts\python.exe tests\step_01_business_walkthrough.py`，预期输出 31 个 `[通过]`、最终打印“演示完成”并以退出码 0 结束；追加 `--keep` 会保留临时工作目录，便于人工打开数据库和镜像文件核对。脚本中每一条断言都对应验收标准中的一个可观察行为，因此人类审查时不需要阅读底层代码。

第二档是面向开发者的底层测试，脚本为 `tests/step_01_technical_test.py`，共 29 个测试。配置部分覆盖默认文件自动生成、五类模型与路径配置的合法更新落盘与观察者通知、十类非法配置（温度越界、向量维度为零、批处理为零、非法设备、零进程、非法 URL、未知字段、重排器开关冲突、未知网关平台、端口越界）的无副作用拒绝、密钥脱敏与“脱敏配置回写不丢密钥”、向量维度守卫与清空后的重建。存储部分覆盖两种存储模式的属性准确性、允许白名单与越界拒绝、上传上限与二进制流校验、流式读取、复制、移动、本地路径定位、宿主文件不可被存储层删除、重复登记同路径保持 UID、外部移动后的哈希路径同步、哈希歧义拒绝、内容变化时拒绝路径同步。元数据部分覆盖第二层字段回填、三类模态摘要互斥、视觉标签与表头列名倒排、主题/关键词/实体/扩展名/大小/名称/业务时间/模态字段的复合筛选、业务日期区间与标签类型校验、统计报表。索引部分覆盖中文 BM25、英文 BM25、表格文本命中、向量近邻排序、带元数据过滤的向量检索、页码范围读取、切片替换后旧词失效、维度不匹配拒绝、重复切片序号拒绝、镜像文件生成与逐行解析、表格 HTML 剥标签、镜像反向映射、禁用镜像、切片更新、删除文件时清理切片/镜像/托管字节、内容变化后清理陈旧派生数据。基础设施部分覆盖 WAL、sqlite-vec 版本、复合索引存在性、关闭后重开数据持久化、数据库/托管区/镜像目录热切换，以及不支持的驱动被拒绝。运行命令为 `.venv\Scripts\python.exe -m pytest tests\step_01_technical_test.py -v`，预期结果为 `29 passed`，实测约 4.6 秒。

除脚本外，人工还可直接检查数据库和镜像文件：用任意 SQLite 客户端打开 `data/index/knowledge.db`，查看 `files`、`tags`、`file_tags`、`chunks` 与 `chunk_fts` 表；到 `data/mirror/<uid前两位>/<uid>.txt` 查看镜像行；到 `data/managed/` 查看托管上传文件。上述检查与脚本断言一致。

### 3）对开发计划和前序步骤已完成代码的调整（如有）

本步骤开始时 `docs/PROGRESS.md` 实际是一个 0 字节的小写文件 `docs/progress.md`，没有任何前序步骤成果，因此本次从步骤一开始，并将文件重命名为计划要求的 `PROGRESS.md`；重命名只涉及文件名，没有改动任何已有内容。

中文全文检索方案在计划中只写了“FTS5 配合中英文分词器”，但 Python 自带 SQLite 的 `unicode61` 分词器会把连续中文当成一个 token，直接使用会导致中文检索失效。经与用户确认，本实现改为先用 jieba 对文本和查询做一致性分词，再把分词结果以空格连接写入 FTS5；这样中英文都使用同一套词流，BM25 的词频统计有效。该方案对单个汉字的查询覆盖有限，因为 jieba 词流中不一定存在单字 token；计划中的 ripgrep 链路正是这类精确子串需求的补充，后续步骤无需为此改动本层接口。

“向量维度一致性校验”在计划中没有定义具体语义，本实现将其明确为三条规则：配置维度必须为正；已有向量时禁止直接修改维度，必须显式清空切片/向量后重建；每次写入切片向量与查询向量时逐条校验维度。这样既能在配置更新阶段拦截错误，也能在索引写入阶段拦截调用方错误。

“宿主机原地引用模式的路径白名单”在计划中没有定义默认行为。经与用户确认，空白名单表示不限制路径，适合本机单用户首发；一旦配置白名单，登记、复制、移动的目标与来源都必须位于白名单根目录内，越界会抛出 `PathNotAllowedError`。所有路径都会展开环境变量、转绝对路径并解析符号链接，避免通过 `..` 或链接绕过白名单。

三类模态专属摘要“互不冗余”的要求被实现为数据库层的单列约束：文件只能有一个 `modality_summary_kind`，已有办公摘要的文件再写图像摘要会抛出 `ModalitySummaryConflict`，同类型摘要可以反复覆盖更新。这个约束比在应用层做判断更可靠，后续步骤不会因为并发或代码路径不同而产生多类摘要并存。

配置路径与数据目录的默认语义被明确为：不传参数时数据根目录为项目下 `data/`，可用 `EASYOFFICE_HOME` 覆盖；配置文件默认为 `<数据目录>/config.yaml`，其中相对路径都相对于数据目录解析。运行时修改数据库路径会打开新数据库但不迁移旧数据，修改托管工作区或镜像目录也不会搬移已有文件；这是轻量首发的有意取舍，后续如需迁移应单独实现迁移工具。

本实现只支持 `sqlite` 向量/全文索引驱动与 `local` 对象存储驱动，配置中出现其他驱动会被核心启动或配置热更新拒绝；但 `HybridIndexStorage` 与 `FileStorage` 两个抽象接口已经按计划预留了 Elasticsearch、MinIO 等扩展点，未来只需新增适配器并放宽驱动校验，不需要改动上层调用。

配置中心的“接口连通性探测”被实现为无副作用的探测：OpenAI 兼容服务请求 `<base_url>/models`，Ollama 请求 `<base_url>/api/tags`，本地模式检查权重目录中是否存在实际文件（仅存在空目录不算配置完成），本地解析权重探测同时返回设备与 OCR 开关信息。探测失败只返回 `unreachable`/`not_configured`/`disabled` 等结构化结果，不会抛出网络异常，也不会修改配置。

在核心装配层还补充了几项计划未明确但影响正确性的行为：同一路径重新登记且内容哈希变化时，`EasyOfficeCore.register_inplace` 会主动清理该文件已有的切片和伴生镜像，防止检索在重新解析前返回陈旧内容；切片的 `plain_text` 列强制保存剥标签后的纯文本，`structured_text` 保留原始 HTML，避免 HTML 标签污染 FTS 与镜像；镜像关闭时会删除对应文件的旧镜像；数据库增加了扩展名+修改时间、扩展名+大小、存储模式+修改时间、扩展名+业务时间等复合索引，以贴合计划中的复合筛选场景；本地解析权重探测改为检查目录内容而不是目录存在性。

另外需要说明的是，本步骤不包含任何真实文档解析、OCR、视觉理解、语义提炼或嵌入模型调用：这些分别属于步骤二至五。因此本步骤的切片与向量由测试脚本人工构造，用于验证存储、索引与检索契约；步骤二至四只需按 `ChunkInput` 的结构填充解析结果并调用 `upsert_chunks`/`replace_file_chunks` 即可。

### 4）给后续步骤的开发说明

本步骤对外暴露的主要入口是 `EasyOfficeCore`（`easy_office/core.py`）。后续步骤应优先通过它获取组件，而不是各自重复连接数据库：`EasyOfficeCore.bootstrap(config_path=None, data_dir=None, config_patch=None)` 负责创建配置中心、目录、数据库和全部服务；实例属性 `config`、`storage`、`db`、`metadata`、`index`、`mirror` 分别暴露各层；便捷方法包括 `register_inplace(path)`、`ingest_upload(file_name, stream, fs_created_at=None, fs_modified_at=None, relative_path=None)`、`sync_moved_path(file_uid, new_path)`、`move_file(file_uid, destination)`、`delete_file(file_uid, delete_managed_bytes=True)`、`add_chunks(file_uid, chunks)`、`search_bm25(query, file_filter=None, top_k=10, operator="OR")`、`search_vector(query_vector, file_filter=None, top_k=10)` 和 `close()`。`EasyOfficeCore` 支持上下文管理器。

配置中心的公开接口为 `get()`、`public_config()`、`raw_config()`、`update(patch, persist=True)`、`set(path, value)`、`replace(config)`、`reload()`、`subscribe(listener)`、`add_validator(validator)`、`paths()`、`ensure_directories()`、`resolve_path(value)` 与 `test_connectivity(target="all")`。`public_config()` 是唯一适合暴露给客户端的形式；`raw_config()` 包含真实密钥，不应出现在网络接口或日志中。`paths()` 返回解析后的 `ResolvedPaths`，后续守护进程和 CLI 可以直接用它定位数据目录、配置、数据库、托管区、镜像区和权重目录。配置监听器接收新的 `AppConfig` 深拷贝；自定义校验器接收 `(old, new)` 并可抛异常阻止更新。

存储层的接口是 `FileStorage` 抽象（`storage/base.py`）与默认实现 `LocalFileStorage`（`storage/local.py`）。URI 当前格式为 `local:<绝对路径>`，`StoredObject` 同时携带 `local_path` 与 `uri`，后续归档工具移动文件时应使用 `core.move_file` 或 `storage.move`，移动后调用 `core.sync_moved_path` 或 `metadata.update_path`，以保持数据库路径与磁盘一致。宿主机原地模式的登记不会复制字节；跨端托管模式的文件位于 `storage.managed_workspace`。对于托管上传，`parent_dir` 是实际托管目录，`relative_path` 记录客户端提供的原始相对路径；对于原地文件，只有配置了白名单根目录时 `relative_path` 才有值。未来接入 MinIO 时实现同一 ABC，并把 `core` 中的存储装配替换成新驱动即可。

元数据中心的接口集中在 `MetadataStore`。`register_file(obj)` 对同一 `(storage_mode, local_path)` 做更新而不是重复插入；`update_semantic_metadata(file_uid, topic_tags=..., summary=..., entities=..., keywords=..., business_start_date=..., business_end_date=...)` 支持部分更新，未传入的字段保持不变，传入 `None`/空列表表示清空；`update_modality_summary(file_uid, kind, summary, extra_tags=None)` 中的 `kind` 只能是 `office`/`image`/`table`，`extra_tags` 只接受 `visual`/`column` 两个命名空间；`clear_modality_summary(file_uid)` 用于重新挂接另一种模态摘要。复合查询使用 `FileQuery`，支持扩展名、MIME、存储模式、名称子串、父目录前缀、大小区间、修改时间区间、主题标签（任意/全部）、关键词、实体、业务时间重叠、模态类型和任意模态 JSON 字段过滤。`build_filter_sql(query, alias="fi")` 返回条件与参数，供步骤六的四路检索在 `files` 表联查中复用，调用方不应自行拼接过滤 SQL。

索引层的接口是 `HybridIndexStorage`（`indexing/base.py`）与默认实现 `SQLiteHybridIndexStore`（`indexing/sqlite_store.py`）。步骤二至四构造切片时应填写 `ChunkInput` 的 `chunk_order`、`plain_text`、`structured_text`、`chunk_type`、页码/工作表/行列、`bbox`、`image_path` 和可选的 `embedding`；`chunk_order` 在同一文件内唯一，同一序号再次写入会替换旧切片并同步更新 FTS、向量和镜像。`search_bm25` 返回按 BM25 排序的 `SearchHit`，`search_vector` 返回按归一化余弦相似度排序的 `SearchHit`，两者都携带完整的 `ChunkRecord` 与 `FileRecord`，后续步骤可直接从中取源文件路径、页码、坐标框、结构化原文和命中链路。`get_chunks(file_uid, page_start=..., page_end=...)` 为步骤七的“定向翻页精读”提供了连续页面读取能力。

伴生镜像的接口是 `MirrorCache`（`indexing/mirror.py`）。`sync_chunks(file_uid, chunks)` 由索引层在切片变化时自动调用，后续解析管道通常不需要手动写镜像；若某个步骤需要写入非切片来源的镜像文本，可以使用 `MirrorLine` 与 `write_lines`。ripgrep 链路命中镜像行后，用 `MirrorLine.parse(line)` 解析前缀，再调用 `mirror.describe_hit(line, metadata.get_file)` 得到源文件路径、页码、工作表、行列范围与原文；镜像行格式是 `@@EO|<uid>|<kind>|<page_start>|<page_end>|<sheet>|<row_start>|<row_end>|<col_start>|<col_end>|<text>`，工作表名经过百分号编码，正文在最后一个字段。

数据库层的 `Database` 暴露 `transaction()`、`execute()`、`fetchone()`、`fetchall()`、`embedding_dim`、`vector_count()`、`set_embedding_dim()`、`reopen()` 等能力。后续步骤如果直接使用数据库，应遵守“写入走 `transaction()`、读取走 `fetchone/fetchall`”的约定；步骤五的多进程索引必须按计划把各工作进程的解析结果回传主进程，由主进程用单连接批量写入，避免多进程并发写 SQLite。当前 `Database` 内部有可重入锁，适合守护进程的线程池，但不适合多个进程各自持有连接写入。

已知遗留问题与注意事项如下。第一，本步骤没有实现任何真实解析器、OCR、VLM 调用、语义提炼和嵌入模型，步骤二至五接入真实组件后，切片与向量的质量才具备实际意义。第二，修改 `embedding_dim` 时如果已有向量会被拒绝；需要先删除或重建切片，再修改配置。第三，修改数据库路径会打开一个新的空数据库，不会迁移旧数据；修改托管区或镜像目录也不会搬移已有文件，这是首发的轻量取舍。第四，敏感密钥以明文保存在本地 YAML 中，仅在 `public_config()`/网络导出中脱敏，部署时应限制配置文件权限。第五，中文 BM25 依赖 jieba 词流，单字查询和未登录词可能无法命中，复杂正则与精确子串应由步骤六的 ripgrep 链路承担。第六，镜像文件目前是按整文件重写，超大文档的增量镜像写入性能需要在后续步骤评估。第七，标签匹配在 SQLite 中区分大小写，英文标签如需大小写不敏感匹配，应在后续检索层统一规范化。第八，`FileQuery.modified_after/modified_before` 接受 ISO-8601 字符串、`date` 或 `datetime`，内部统一转换为 UTC 时间戳字符串；其他无法解析的时间字符串会抛出 `MetadataValidationError`。第九，Windows 上使用 `st_ctime` 作为创建时间，Linux 上优先使用 `st_birthtime`，若文件系统不提供出生时间则回退到 `st_ctime`，两种平台的“创建时间”语义可能略有差异。第十，本步骤的连通性探测只验证服务可达性或权重文件存在性，不验证模型名称是否真的可用，真正的模型调用与错误处理留给后续在线步骤。
