# 项目 MASTER

[English](MASTER.en.md)

## 目的

定义稳定的项目目标、约束、工作图和完成门。动态任务状态属于 `STATUS.md`
和本地状态库。

## 约束

- 当前代码和版本化证据优先于历史记忆；
- 已完成或拒绝的工作不能在没有 correction 的情况下复活；
- 外部副作用需要 active claim、expected revision 和 validator；
- 节省 token 不能降低构建、测试、安全或证据门。

## Work

| ID | 状态 | 结果 | 依赖 | 完成门 |
|---|---|---|---|---|
| work-initial | planned | 定义第一个可度量结果 | none | evidence recorded |

## Release Gates

- 必需工作已完成；
- 当前证据解决所有完成声明；
- repository verification 通过；
- 不包含 secret、个人路径、私有标识或原始 transcript。
