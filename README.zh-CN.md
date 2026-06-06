# memory-doctor

> 🌐 [English](README.md) | 简体中文

AI 智能体长期记忆系统的健康检查工具。

- **仅使用标准库。** 无需安装，无依赖，不联网。
- **单文件。** `scripts/memory-doctor.py`，约 1300 行。
- **默认安全。** 只读模式；`--fix` 只作用于明确选择参与的检查项。
- **CI 友好。** 退出码区分“存在问题”（1）和“密钥泄露”（2）。

```bash
$ python3 scripts/memory-doctor.py --scan --quiet
⚠️  3 finding(s), worst=critical
```

默认情况下，输出中的密钥子串会被遮蔽为
`<REDACTED:CODE>`，以防报告里的真实 token 在粘贴输出时再次泄露。
使用 `--no-redact` 可以显示完整值（仅在输出仅在受信通道中传递时使用）。

## 它能捕获的问题

| Code | Severity | Example |
|---|---|---|
| `SECRET-LEAK` | 🔴 critical | 把 GitHub PAT 粘贴进了日记 |
| `DUPLICATE-KEY` | 🟡 medium | `**Timezone:**` 在两个章节中以不同值重复定义 |
| `DANGLING-REF` | 🟡 medium | 本体关系指向了已删除的实体 id |
| `BUDGET-MEMORY` | 🔵 low | `MEMORY.md` 超过 300 行 |
| `BUDGET-SECTION` | 🔵 low | 单个章节超过 50 行 |
| `STALE-ITEM` | 🔵 low | LRN/ERR 条目超过 90 天未出现 |
| `FILE-MISSING` | ⚪ info | 缺失核心引导文件 |
| `ONTOLOGY-STRUCT` | 🟡/⚪ | `graph.jsonl` 格式错误或缺少 schema |
| `EMPTY-HEADER` | 🔵 low | `.learnings/*.md` 中 `## 标题` 下无正文 |
| `BUDGET-MEMORY-SOFT` / `HARD` / `CRIT` | ⚪/🔵/🟡 | 200 / 300 / 500 行三档分级提醒 |
| `ONTOLOGY-ISOLATED` | 🔵 low | 本体节点没有任何关系 |
| `DAILY-MEMORY-NAME` | 🔵 low | `memory/*.md` 文件名不符合 `YYYY-MM-DD.md` 格式 |

每项检查的完整语义见 [`docs/DESIGN.md`](docs/DESIGN.md)。

使用 gitignore 风格的
[`.memory-doctorignore`](docs/QUICKSTART.md#4b-silence-known-false-positives) 屏蔽已知的误报。

## 快速开始

```bash
# 1. 获取文件
git clone https://github.com/Leorand-dev/memory-doctor.git
cd memory-doctor

# 2. 运行
python3 scripts/memory-doctor.py --scan

# 3. （可选）pre-commit 钩子
git config core.hooksPath scripts/hooks
```完整说明见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)。

## 测试

```bash
python3 -m unittest scripts.tests.test_memory_doctor
# Ran 53 tests in ~6s
# OK
```

## CI / GitHub Actions

项目在 `action/` 下提供了官方 GitHub Action。可直接接入任何带 Python 工作区的项目：

```yaml
- uses: Leorand-dev/memory-doctor@v1
  with:
    workspace: .
    fail-on: medium
    redact: "true"
    exclude: scripts/tests
```

输入、输出以及退出码到 job 状态的映射见 [`docs/ACTION.md`](docs/ACTION.md)。

## 设计原则

1. **传感器，不是执行器。** doctor 只负责报告，由用户决定如何处理。
   唯一的例外是 `DANGLING-REF`，因为它显然是图谱卫生层面的修复，且必须显式使用 `--fix` 才会执行。
2. **密钥是神圣的。** `SECRET-LEAK` 是最高严重级别的问题，
   **在任何 flag 下都不会被自动清除**。退出码 `2` 专供密钥使用，
   让 CI 可以在不解析 JSON 的情况下直接 fail-loud。
3. **不引入新依赖。** doctor 只依赖 Python 3.8+ 标准库，别无其他。Vendoring 只需复制这一个文件。
4. **高精确率优于高召回率。** 误报会让用户养成忽略 doctor 的习惯。密钥相关的正则因此被刻意收窄。

## License

MIT. See [`LICENSE`](LICENSE).
