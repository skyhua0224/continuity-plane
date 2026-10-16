# Python API

[English](api.en.md)

采用者从 [CLI 工作流](workflows.md)开始。MCP 的 `continuity_help` 导出参数，`continuity_plan` 覆盖任务卡、Todo、规则、来源等元数据操作；这些调用不要求先绑定 State Session。`continuity-mcp` 与 `continuity-search-mcp` 是两个独立的 stdio 入口，安装／远端路径见 [部署](deployment.md)。

命令行提供本地初始化、完整性检查和授权状态读取：

```bash
continuity init --root . --project-id my-project
continuity verify --root .
continuity doctor --root .
continuity state show --root .
```

更底层的模块提供带版本的集成合同：

```python
from continuity_plane.sqlite_state_store import SQLiteStateStore
from continuity_plane.source_registry import SourceRegistry

registry = SourceRegistry("project-a")
store = SQLiteStateStore(".continuity/state.sqlite3")
snapshot = store.read_project("my-project")
```

状态集成应使用服务边界，不应直接修改 snapshot。每个权威操作都需要可信
request context、expected revision 和稳定 request ID。

PostgreSQL adapter 通过可选依赖提供：

```python
from continuity_plane.postgres_state_store import PostgresStateStore

store = PostgresStateStore("postgresql://localhost/context")
store.initialize()
```

包处于 alpha 阶段，未列入 API 文档的 import path 可能在稳定版前调整；wire
schema 会保留明确版本和迁移要求。
