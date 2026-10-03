# Continuity 简明技术中文

Continuity uses ASD-STE100 principles as an output rendering layer. It does not
control execution and it does not require a report after each tool call.

## Rules

- One sentence states one fact or one action.
- Use a fixed term for one concept. Do not alternate between synonyms.
- State the subject, object, condition, and result explicitly.
- Use fixed status words: `未开始`, `进行中`, `已完成`, `阻塞`, `待决策`.
- Replace vague words such as “适当处理” and “继续看看” with a command or a gate.
- Report only at TodoQueue completion, a real blocker, or a required decision.
- A long command does not create a progress report. The next action remains in the queue.

## Example

```text
状态：进行中
当前事项：实现 Win32 file backend
已完成：POSIX focused test 通过
下一动作：新增 win32_file_open() 并运行 focused test
完成条件：Windows 构建成功，测试返回 0
阻塞：无
```

The rules are intentionally short. They are applied at report boundaries and
TodoQueue context injection, not loaded as a permanent skill.
