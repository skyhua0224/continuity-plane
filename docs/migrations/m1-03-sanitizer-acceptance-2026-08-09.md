# M1-03 Sanitizer Acceptance

版本：1  
日期：2026-08-09  
状态：🧑‍💻（实现与离线验收完成；真实 fixture 仍受 admission policy 约束）

## 范围

本叶提供确定性文本 sanitizer 和 replay/typed admission gate。已覆盖 secret、PII、机器专有路径和 SPDX license 标识四类已知注入；原始 transcript、真实用户值和 provider archive 未被读取或写入仓库。

## 验收证据

运行命令：

```text
python3 -m unittest discover -s tests -v
```

当前结果：17 tests passed，其中 5 个为 M1-03 sanitizer/admission 测试。

通过条件：

- secret、email/phone、已知用户目录和未批准 SPDX license 的值不出现在 sanitized text 中；finding 只记录类别和位置，不记录匹配原文。
- 同一输入得到 byte-equivalent sanitized text、finding 集合和 SHA-256。
- 任何 finding 或无效 provenance 都不能通过 `validate_admission`；干净内容与有效 provenance 才允许继续。
- 允许的 SPDX license 由显式 allowlist 控制；未声明许可不能静默进入 fixture。

## 后续边界

当前规则是 M1-04 前的基线分类器，不能证明所有供应商格式或所有敏感类别已覆盖。M1-04 必须使用受控 archive 的最小 byte range 扩展注入 corpus，并由 independent validator 复核漏检率；任何漏检都会阻止 fixture admission。
