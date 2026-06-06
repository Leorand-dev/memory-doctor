# memory-doctor. 快速开始

> 🌐 [English](QUICKSTART.md) | 简体中文

Agent 长期记忆系统的健康检查。仅依赖标准库,无需安装。

## 1. 获取文件

克隆本仓库,或将 `scripts/memory-doctor.py`(单个文件,约 1300 行)复制到你的项目中。

```bash
# 选项 A:克隆仓库
git clone https://github.com/Leorand-dev/memory-doctor.git
cd memory-doctor

# 选项 B:只抓取文件
curl -O https://raw.githubusercontent.com/Leorand-dev/memory-doctor/main/scripts/memory-doctor.py
chmod +x memory-doctor.py
```

## 2. 运行

```bash
# 默认:只读扫描,人类可读输出
python3 scripts/memory-doctor.py --scan

# 机器可读,便于接入其他工具
python3 scripts/memory-doctor.py --scan --json | jq '.summary'

# 单行输出,用于 cron / heartbeat / pre-commit
python3 scripts/memory-doctor.py --scan --quiet
```

## 3. 接入工作流

### Pre-commit 钩子(推荐)

```bash
mkdir -p scripts/hooks
cp scripts/hooks/post-commit.example scripts/hooks/post-commit
chmod +x scripts/hooks/post-commit
git config core.hooksPath scripts/hooks
```

该钩子在每次提交后运行:发现问题时打印一行摘要,遇到严重问题(密钥泄露)时输出完整报告,但**绝不会阻塞提交**。若需跳过单次提交:

```bash
SKIP_MEMORY_DOCTOR=1 git commit -m "..."
```

### CI 门禁

```yaml
# GitHub Actions 示例
- name: memory-doctor
  run: |
    python3 scripts/memory-doctor.py --scan --quiet --exclude scripts/tests
  # exit 0 = 干净,1 = 有发现,2 = 密钥泄露(构建失败)
```

### 每周 cron / heartbeat

```bash
# /etc/cron.d/memory-doctor
0 9 * * 1  cd /path/to/workspace && python3 scripts/memory-doctor.py --scan --quiet --exclude scripts/tests || echo "memory-doctor flagged issues" | mail -s "memory-doctor: $(hostname)" leo@example.com
```

## 4. 阅读设计文档

完整的语义说明(每项检查的作用、为何部分问题故意不自动修复、如何扩展 doctor),请参阅 [`docs/DESIGN.md`](DESIGN.md)。

## 4b. 屏蔽已知的误报

在工作区根目录添加 `.memory-doctorignore`(gitignore 风格):

```gitignore
# 屏蔽所有 SECRET-GHP 发现
code:SECRET-GHP

# 屏蔽 memory/archive/ 下的所有发现
path:memory/archive

# 屏蔽特定发现(code + path + line)
code:STALE-ITEM path:.learnings/LEARNINGS.md:40-50

# 一行多个条件:必须同时匹配 code 和 path
code:DUPLICATE-KEY path:MEMORY.md:9
```

被屏蔽的发现仍会打印(带 `[suppressed]` 标签与匹配的规则),以保留完整审计轨迹,但**不计入**最严重等级的退出码。

使用 `--ignore-file path/to/other` 可指定其他文件名(便于在 CI 中测试规则)。

## 5. 运行测试

```bash
python3 scripts/tests/test_memory_doctor.py
# Ran 53 tests in ~6s
# OK
```

测试是自包含的(使用 `tempfile.mkdtemp()` 创建临时夹具),无外部依赖。

## Doctor 检查项

| Code | Severity | Auto-fix? | Catches |
|---|---|---|---|
| `STALE-ITEM` | low | no | LRN/ERR 条目超过 90 天未出现 |
| `DUPLICATE-KEY` | medium | no | 同一文件内 `**Key:**` 值冲突 |
| `DANGLING-REF` | medium | yes | 本体图指向不存在的实体 id |
| `SECRET-LEAK` | critical | **NEVER** | 磁盘上出现 ghp_/sk-/xoxb-/PEM/Bearer 模式 |
| `BUDGET-MEMORY` | low | no | MEMORY.md 超出行数预算 |
| `BUDGET-SECTION` | low | no | 某个 section 超出行数预算 |
| `FILE-MISSING` | info | no | 核心 bootstrap 文件缺失 |
| `ONTOLOGY-STRUCT` | medium/info | no | graph.jsonl 格式错误或 schema 缺失 |

## 退出码

| Code | Meaning |
|---|---|
| 0 | 干净(无发现,无屏蔽) |
| 1 | 有未屏蔽的发现 |
| 2 | **密钥泄露**(始终失败) |
| 3 | 内部错误 |
| 4 | 干净,但有 `N` 条发现被 `.memory-doctorignore` 屏蔽 |

退出码 4 让 CI 门禁可以区分"工作区健康"(0)与"工作区健康**但**存在屏蔽规则"(4)。使用 4 来标记工作区累积的屏蔽数量是否出现漂移。

## 当某项检查触发时

1. **阅读提示信息和建议。** 它们写得很具体。
2. **打开报告中的 path:line 定位文件。**
3. **自行判断。** Doctor 是传感器,不是执行器。它从不删除数据,也从不轮换凭证——这些由你来做。
4. **对于密钥泄露:** 先轮换凭证,再删除泄露的代码行。只删除不轮换比什么都不做更糟。

## 许可证

MIT。详见 `LICENSE`。
