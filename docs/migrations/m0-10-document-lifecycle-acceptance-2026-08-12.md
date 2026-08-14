# M0-10 Documentation Lifecycle Acceptance

版本：5  
日期：2026-08-14  
状态：verified

## 范围

`context.document-control-manifest/v1alpha1` 固定规范文档的 path、category、revision、content hash、容量指标、authority declaration、change receipt、supersedes 和 evidence reference。manifest 由仓库文件确定性生成；repository verifier 离线重算并拒绝漂移。

## 验收结果

- STATUS/content evidence hash drift、过期 evidence、跨文档全文重复、非连续 supersedes、多个 active leaf 和恢复引用循环均 fail-closed；
- `STATUS.md` 容量从 15,884 bytes 降至 3,750 bytes，下降 76.3913%，门限为 12 KiB；
- MASTER 执行台账按 Campaign 拆为独立二级节；全文件最大二级节从 28,976 bytes 降至 8,988 bytes，下降 68.9812%，门限为 24 KiB；
- active work、status、blocker、next action 与 governance revision 恢复字段为 5/5；STATUS 恢复引用图由受管链接重算为 2 hops；
- MASTER 是唯一可声明 governance authority 的文档；所有文档 active-state authority 为 false；report/projection 必须绑定 source state revision、MASTER digest、template version 与 content hash，state-write authority 固定为 false；
- verifier 不联网检查外部 URL；外部 reference freshness 继续由 versioned catalog 与 M7-06 watcher 验收；
- Markdown authority receipt 只验证声明合同，真实授权身份与 diff-aware admission 仍由 M0-11/M8-05 验收。
- 53 份 managed document 当前 hash/metrics/evidence ref 一致；public validator has no provenance-bypass parameter。每次验收执行 one full provenance preflight plus 40 content-only samples；预检覆盖 Git lineage 与 supersedes provenance，计时样本只验证已预检 manifest 的内容合同；failures 为 0、p95 小于 100 ms；未认证的历史时延不写入 receipt；
- benchmark baseline、样本数和 p95 门由 strict governance config 固定；baseline 必须是 `HEAD` 可达 commit，Git blob 读取受 2 秒和 8 MiB 上限约束；
- 独立合同复审覆盖 authority、projection、strict schema、全仓 Markdown 发现、supersedes provenance、bounded Git read 和 benchmark receipt；最终结论为 High 0、Medium 0。后续可重复性修正固定 alternate baseline 选择，并将 Git lineage 预检移出每个 live 样本；完整 lineage 门保留在普通 repository validation，公开接口与私有内容校验均不存在调用方可传入的 provenance bypass；
- 版本：4 的公共入口将调用方 manifest 冻结为 ordinary built-in dict/list snapshot；同一快照依次经过本地结构门、Git lineage、内容合同与 supersedes provenance。cycling documents view、empty documents and malformed change receipts 均由回归测试拒绝；40 个内容样本复用已完成预检的同一快照。

## 验证命令

```bash
.venv/bin/python -m unittest tests.test_m0_10_document_lifecycle -v
.venv/bin/python -m unittest tests.test_m0_10_document_benchmark -v
.venv/bin/python tools/verify_repository.py --root .
```

原始 transcript、供应商档案和未脱敏 evidence 均未进入 Git。Markdown authority receipt 只证明结构合同；真实 actor authorization 与 diff-aware admission 仍由 M0-11/M8-05 验收。
