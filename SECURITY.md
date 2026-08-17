# 安全策略

[English](SECURITY.en.md)

## 漏洞报告

请通过获得本软件的仓库私下向维护者报告漏洞。可能暴露凭据、私有源码、状态
记录或外部副作用的问题不得提交为公开 Issue。

Canonical 仓库使用 GitHub 私有漏洞报告：
<https://github.com/skyhua0224/continuity-plane/security/advisories/new>。

## 安全边界

- Provider 输出和 memory candidate 没有 State 写权限；
- State 写入需要授权、expected revision 和 validator；
- 外部副作用需要当前 claim、scope ownership 和幂等键；
- 原始 provider transcript 和凭据不得进入 Git；
- 公开 artifact 必须扫描 secret、个人路径和私有标识。

默认本地 profile 不监听网络端口。共享部署必须根据环境增加 transport 认证、
tenant/project 隔离、审计保留、备份和恢复机制。
