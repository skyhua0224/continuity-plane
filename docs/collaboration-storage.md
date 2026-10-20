# Gitea 协作模式：仓库声明与本机数据

版本：1；适用 Alpha29。此合同规定共享内容、成员本地配置与权威边界。操作命令见 [Gitea 协作](gitea-collaboration.md)。[English](collaboration-storage.en.md)

## 写入位置与权限

| 内容 | 位置 | Git／跨成员共享 | 权威与维护方式 |
|---|---|---|---|
| 实例、仓库、凭据逻辑引用 | 仓库 `.continuity/collaboration.toml` | 提交；私有仓库名称也按项目保密要求处理 | 共享连接声明；不含 Token、用户名密码或本机路径 |
| 规则、验收约定、Issue／PR 模板 | 版本化 `docs/collaboration/`、`AGENTS.md`、`.gitea/` | 提交 | 规则由 Git 评审；不把动态任务状态写进规则 |
| 任务目标、验收、依赖、驱动者、阻塞、交接 | Gitea Issue 正文与事件评论 | 通过 Forge 共享 | 唯一共享任务权威 |
| 分支、提交、真实评审请求／结果、合入事实 | Gitea PR／Git | 通过 Forge／Git 共享 | 待评审或 PR 关闭不表示已交付 |
| 本机注册、绝对 checkout 路径 | 用户数据根 `projects/<local-project-id>/project.json` | 不提交、不复制到别人的机器 | CLI register 管理；同一远端可以有不同本地项目 ID |
| Issue 快照 | 用户数据根 `projects/<id>/gitea/<scope>/cache/` | 不提交、不作为共享账本 | 明确 cache-only；不可拿来证明当前认领或合入 |
| 待发布／结果未知操作 | 同 scope 下 `operations/` | 不提交、不跨机重放 | 保留 operation-id；未知结果只对账、不盲目重发 |
| 成员自己的同步进度 | 同 scope 下 `consumers/` | 不提交、不共用文件 | 完整读完分页再 ACK，未确认可重放 |
| 压缩恢复、代码索引、用量、报告、工作目录 | 本机恢复／数据／缓存根，必要时仓库 `.continuity/` 的本机侧车 | 不提交；报告中仅有获准项目摘要才发布到 Issue | 不成为第二份共享任务权威；原始会话不进入 Git |
| 全局规则、参考资料登记 | 用户数据根 `global-rules/`、`references/` | 默认仅本机 | rule/reference 的 global scope，不自动向队友同步 |
| 原生登录、Token、账号与模型会话 | 成员自己的 tea／AI 客户端配置 | 不提交、不搬到别的机器／profile | Continuity 调本机 tea，不读取或导出其 Token 配置 |

`<scope>` 是实例、仓库和 credential_ref 的本地派生键，不是团队账号数据库。成员更换 AI 不改变 Issue 驱动者；handoff-request 不等于对方接受。每位成员只使用自己的本机登录。

## 仓库 `.continuity/` 的最小布局

```text
repository/
  .continuity/
    .gitignore
    collaboration.toml
    controller-profile.json # 可选：共享交流与报告约定
    knowledge/*.hash.json   # 可选：批准并人工评审的项目知识
  docs/collaboration/       # 可选：版本化规则和评审约定
  .gitea/                  # 可选：Issue / PR 模板
  AGENTS.md                # 可选：开工协议
```

```toml
# .continuity/collaboration.toml：Alpha29 实际支持的字段
[collaboration]
backend = "gitea-issues"
instance = "https://forge.example.com:3443"
repository = "team/sample"
credential_ref = "tea:work"
```

实例可带 HTTPS 端口／子路径。credential_ref 是约定的本地 tea 登录别名，每个人可各自建立名为 work 的登录，不能提交真实 Token。此文件没有 data_root、全局登录或模型凭据字段；不要加入看似生效的未支持字段。

Alpha29 的 backend configure 在缺少 `.continuity/.gitignore` 时创建下列内容，已有文件原样保留：

```gitignore
# Shared declarations and reviewed knowledge only; runtime data stays member-local.
*
!.gitignore
!collaboration.toml
!controller-profile.json
!knowledge/
!knowledge/*.json
```

仓库上层若整体忽略 `.continuity/`，显式审核后只添加这两个文件：

```bash
git add -f .continuity/.gitignore .continuity/collaboration.toml \
  .continuity/controller-profile.json .continuity/knowledge/REVIEWED.json
git diff --cached --name-only
```

不要 `git add -f .continuity/`。不要在这里新增可独立改写的 tasks.json、共享 Todo、session 目录或成员凭据。既有 MASTER／STATUS 可以保存规范或注明生成来源的投影，不能作为 Issue 的可写替代账本。开启 Gitea 不自动上传、迁移或删除旧的本地任务。

## 全局目录：默认值与 `~/.continuity`

“全局”表示当前 OS 用户／所选 profile 的本机范围，不表示团队共享服务。默认数据根按以下顺序选择：

1. 显式命令的 `--data-root`（仅对支持该参数的命令）。
2. `CONTINUITY_DATA_ROOT`。
3. `$XDG_DATA_HOME/continuity`。
4. `~/.local/share/continuity`。

`~/.continuity` **不会自动被当成全局配置读取**，没有隐式 `~/.continuity/config.toml`。如果希望统一使用这个名字，可在该成员自己的 shell／MCP server 环境显式配置：

```bash
mkdir -p "$HOME/.continuity"
chmod 700 "$HOME/.continuity"
export CONTINUITY_DATA_ROOT="$HOME/.continuity"
continuity collaboration register --project-id demo --control-root /workspace/sample
continuity collaboration backend doctor --project-id demo --format json
```

切换数据根相当于选择另一份本机注册与游标；不自动迁移旧数据。不要未经核对就复制账号或另一成员的数据根。保持路径持久、成员私有，不放在 tmpfs、`/tmp` 或 `/var/tmp`。不同本地身份可以选择独立数据根；CLI 和对应 MCP 使用同一选择，不能一端写默认根、另一端读自定义根。

MCP 的最小 server 环境示例（命令须在当前成员电脑执行）：

```json
{
  "command": "/absolute/path/to/continuity-mcp",
  "env": {"CONTINUITY_DATA_ROOT": "/absolute/member-local/continuity-data"}
}
```

这是宿主的 stdio server 配置片段，具体包裹结构依宿主而定，不是项目文件。使用默认数据根时不需要 env。tea 按自己的 XDG 配置查找当前成员的 work 登录；只有你明确选择了另一个本机 profile 时才设置 XDG_CONFIG_HOME，不要为了连接项目而覆盖它，也不把它指向别人的目录。

## 规则与 AI 开工约定

共享规则建议以仓库版本化文档为来源。现有 `rules import/export`、全局规则和参考资料库仍是本机能力；本版不把它们自动写入远端 Issue 或同步到其他成员。若导入本机规则库，应记录来源文件和版本、用 export 检查漂移，避免两个可随意覆盖的来源。

AGENTS.md 可采用以下简短约定：

> 先读 `.continuity/collaboration.toml` 和版本化协作规则；检查 CLI 版本、backend doctor 与 backend sync。任务只在 Issue 更新；认领使用当前成员的本机认证。每次写入携带刚观察的 remote_revision 和稳定 operation-id；REV 是快照哈希，不是服务器 CAS。请求交接／评审后不声称对方已接受。分页读完再 ACK。变更 AI 不改变人类驱动者；合并、发布、部署需要独立授权。不要迁移原生登录、凭据或完整会话数据。

缺安装时使用[启动脚本](../examples/gitea-bootstrap.py)并要求用户明确批准安装来源和持久路径；缺认证时提示在当前机器登录 tea，不索取明文 Token。Gitea 模式不用先 init State、部署数据库或建立永久 controller。
