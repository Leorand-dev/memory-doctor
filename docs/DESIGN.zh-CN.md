# memory-doctor. 设计文档

> 🌐 [English](DESIGN.md) | 简体中文

> 用于智能体长期记忆系统的健康检查。
> 仅依赖 Python 3 标准库,无外部依赖,默认安全。

---

## 1. 动机

长期运行的 AI 智能体会在多个会话之间累积状态:精心维护的长期笔记(`MEMORY.md`)、逐日日志(`memory/YYYY-MM-DD.md`)、进程内学习记录(`.learnings/*.md`)、类型化知识图谱(`memory/ontology/graph.jsonl`)。随着时间推移,这些状态会以微妙的方式腐化:

- **事实过时。** 六个月前的 "Last-Seen" 日期已不足以作为该事实仍然成立的可靠证据。
- **矛盾悄然产生。** 同一个键在两处定义,值却略有不同。
- **悬空引用。** 某个实体被重命名或删除,但某条关系仍指向旧的 id。
- **密钥泄露。** 一次调试会话不小心把真实的 API 密钥粘贴到了日记里。智能体的记忆文件如今就是一个随时可能发生的凭据泄露事件。
- **记忆文件膨胀。** 精心维护的笔记长到人类无法在 60 秒内扫完,违背了"精心维护"的初衷。

`memory-doctor` 是一个单文件、仅依赖标准库的健康检查工具,能在一遍扫描中同时暴露上述五类问题,输出可机读,并提供 CI 友好的退出码。设计目标:

- **严格到足以有用。** 收窄的高精度密钥匹配模式;首次命中即胜出;每条 finding 显式声明 `fixable`。
- **安全到可无人值守运行。** 默认只读;`--fix` 仅作用于明确开启的 finding;密钥永不自动清除,仅报告。
- **轻量到可频繁运行。** 在典型工作区上亚秒级完成,零依赖,无网络,无 LLM 调用。

## 2. 架构

```
scripts/
├── memory-doctor.py          # 750 行,单文件
└── tests/
    └── test_memory_doctor.py # 19 个隔离单元测试,合计约 2 秒
```

脚本刻意保持为**单文件**。没有包结构,没有插件系统,没有配置文件解析器。配置完全通过 CLI 标志完成。这让该工具可以被任何项目轻松 vendor:复制文件,运行,完成。

### 数据模型

```python
@dataclass
class Finding:
    code: str            # 例如 "STALE-ITEM"、"SECRET-LEAK"
    severity: str        # info | low | medium | high | critical
    path: str            # 相对于仓库根的路径
    line: int | None     # 1-indexed;None 表示整文件
    message: str
    suggestion: str = ""
    fixable: bool = False
```

`severity` 顺序固定:`critical > high > medium > low > info`。`fixable` 按检查项设置;`--fix` 执行器只处理 `fixable=True` 的 finding。`code` 跨版本保持稳定,作为抑制文件和 CI 门禁的长期句柄。

### 检查流水线

```
gather files
   │
   ├──> C6 FILE-MISSING         (info,永不自动修复)
   ├──> C5 BUDGET-MEMORY        (low,永不自动修复)
   ├──> C5 BUDGET-SECTION       (low,永不自动修复)
   ├──> C1 STALE-ITEM           (low,永不自动修复)
   ├──> C2 DUPLICATE-KEY        (medium,永不自动修复)
   ├──> C4 SECRET-LEAK          (critical,绝不自动修复)
   ├──> C3 DANGLING-REF         (medium,可自动修复)
   ├──> C7 ONTOLOGY-STRUCT      (medium/info,永不自动修复)
   │
   ▼
按严重度降序排序 → 渲染 → 退出
```

各检查相互独立,可按任意顺序执行。按严重度排序使 critical 级别的 finding 排在最前,在滚动查看时不会被遗漏。

## 3. 检查语义

### C1 — `STALE-ITEM`

检测 `MEMORY.md`、`.learnings/*.md` 或 `memory/YYYY-MM-DD.md` 中匹配到的文件里的 `## [TYPE-YYYYMMDD-NNN] (kind)` 头。在每条 entry 内查找 `Last-Seen`(首选)或 `Logged`(回退)日期字段。若两者中较早的日期距今超过 `--stale-days` 天,则输出 `STALE-ITEM` finding。

**为何使用两个信号?** `Last-Seen` 是权威的"该条目仍然成立"时间戳;`Logged` 是原始创建日期,在 `Last-Seen` 缺失时作为回退。命中即胜出。

**刻意不采取行动。** 该工具不删除也不移动过时条目。过时 ≠ 错误。用户收到 "verify" 提示后再做决定。

**默认阈值:90 天。** 足够长,以避免日常抖动产生噪声;足够短,使被遗忘的条目在一个季度内得到关注。

### C2 — `DUPLICATE-KEY`

解析以下形式的每一行:

```markdown
- **Key:** value
- Key: value         (无加粗)
- * **Key:** value   (任意列表标记)
```

(格式识别刻意宽松,见 `_check_duplicates` 中的正则。)若同一键在同一文件中出现多次且值不一致,则输出 `DUPLICATE-KEY` finding,列出所有行号和所有值。若值一致,则视为有意的冗余(例如跨节镜像),静默忽略。

**为何是按文件而非跨文件?** 跨文件重复检测噪声大,容易在有意拆分时产生误报。如确需此功能,代码足够小,可直接 fork。

### C3 — `DANGLING-REF`

逐行解析 `memory/ontology/graph.jsonl`。每行形如:

```json
{"op": "create", "entity": {"id": "pers_xxxx", ...}}
{"op": "relate", "relation": {"from": "a", "to": "b", "type": "..."}}
```

当某条 `relate` 的 `from` 或 `to` 引用了一个尚未被 `create`(或在文件中较晚才被 `create`)的 id,则输出 `DANGLING-REF` finding,并指出缺失的 id。

**这是唯一支持 `--fix` 的检查。** 修复会原子地(先写临时文件再 `Path.replace`)重写文件,移除被标记的行。它拒绝触碰任何未被标记的行,因此重复运行 `--fix` 是幂等的。

**已知局限:** 当前实现不处理 `op: "delete"`:一个曾被引用、后来被删除的实体不会被识别为悬空引用。已记为未来的扩展项。

### C4 — `SECRET-LEAK` 🔴

最重要的检查。扫描工作区中**每一个可能的文本文件**(跳过 `.git`、`.openclaw`、`node_modules`、`__pycache__` 以及一组二进制后缀),匹配七种高精度模式:

| Code | Pattern | Catches |
|---|---|---|
| `SECRET-GHP` | `\bghp_[A-Za-z0-9]{20,}\b` | GitHub classic PAT |
| `SECRET-PAT` | `\bgithub_pat_[A-Za-z0-9_]{20,}\b` | GitHub fine-grained PAT |
| `SECRET-OAI` | `\bsk-[A-Za-z0-9]{20,}\b` | OpenAI API key |
| `SECRET-SLACK` | `\bxox[abop]-[A-Za-z0-9-]{10,}\b` | Slack token |
| `SECRET-GOOGLE` | `\bAIza[0-9A-Za-z_\-]{20,}\b` | Google API key |
| `SECRET-PEM` | `-----BEGIN ...PRIVATE KEY-----` | PEM private key 块 |
| `SECRET-BEARER` | `Bearer <40+ chars>` | Header 中的长 bearer token |

**刻意不采取行动,即使加 `--fix` 也一样。** 该工具报告一条 `critical` finding 并以退出码 `2` 退出。永不删除该行,永不重写文件。理由如下:

1. 在轮换凭据前删除密钥,比保留可见更糟。拥有读权限的攻击者现在握有凭据,而操作者以为已删除。
2. 一行形似密钥的内容可能是测试 fixture、文档示例或占位符。自动重写是损坏用户数据的好办法。
3. 用户(或其 CI)才是做"轮换还是删除"决策的合适位置。该工具是传感器,不是执行器。

**为何模式要收窄?** 此处的召回率(recall)若是灾难,误报会训练用户忽略该工具。GitHub 风格 token 设 20 字符最小长度是保守的——真实 token 通常 36+ 字符。没有已知前缀的、看起来像长 base64 的字符串刻意不被标记(噪声太大)。

**退出码矩阵:**

| Code | Meaning | When |
|---|---|---|
| 0 | clean | 无未抑制 finding,未使用抑制 |
| 1 | findings | 至少一条未抑制 finding |
| 2 | secret leaked | 任意未抑制 `SECRET-*` finding |
| 3 | internal error | 扫描过程中出现未预期异常 |
| 4 | clean, but `N` findings were suppressed | `.memory-doctorignore` 命中,且没有未抑制 finding |

被抑制的 finding 不计入最严重档位。若扫描本应清白且确实使用了抑制,我们用退出码 `4` 显式呈现,供 CI 门禁标记漂移。被抑制的密钥不触发退出码 `2`;它计入被抑制档位,转而贡献到退出码 `4`。

**输出脱敏(`--redact`,默认开启)。** 该工具输出中真实的密钥就是二次泄露。默认情况下,finding 的 `message` 中匹配到的、形似密钥的子串会被替换为 `<REDACTED:CODE>`,使操作者仍能在文件中定位泄露点,但 token 本身被隐藏。`--no-redact` 是逃生口,会展示完整值;仅在输出送往可控位置(本地轮换脚本、加密笔记)时使用。v1.1+ 默认开启;v1.1 之前始终展示完整值。

### C5 — `BUDGET`(MEMORY 与 section)

`BUDGET-MEMORY`:整个 `MEMORY.md` 超过 `--max-memory-lines` 行(默认 300)。输出一条低严重度 finding,指向该文件,不带具体行号。

`BUDGET-SECTION`:任何 `## section` 超过 `--max-section-lines` 行(默认 50)。输出低严重度 finding,指向 section 头。若文件中无 `##` 头,则整个文件视为一个隐式 section。

**为何 MEMORY 与按 section 拆开?** 一个 1000 行、有 30 个 section、每个 33 行的 `MEMORY.md` 不是预算问题——只是组织良好的长文档。一个 200 行、但其中某个 section 占 180 行的文件是糟糕的:那个 section 需要拆分。两种检查捕获不同的病态。

### C6 — `FILE-MISSING`

对 `[MEMORY.md, AGENTS.md, SOUL.md, IDENTITY.md, USER.md, TOOLS.md]` 中每个缺失的文件,输出一条 `info` finding。info 严重度意味着:不计入退出码。理由是:"缺失"可能仅意味着用户在子树上运行该工具,或该文件被刻意省略(例如只 vendor 了该工具本身)。

### C7 — `ONTOLOGY-STRUCT`

两种形式:

- `graph.jsonl` 中**JSON 行格式错误** → `medium` finding,带行号。
- **缺失 `schema.yaml`** → `info` finding。Schema 是可选的,因此这只是建议。

## 4. 输出格式

### 人类可读(默认)

```text
memory-doctor @ /home/vmser/.openclaw/workspace
  generated: 2026-06-05T13:50:33Z
  scanned:   5 memory files, 48 text files
  findings:  3 (worst=critical)

── 🔴 CRITICAL ──
  SECRET-GHP  scripts/tests/test_memory_doctor.py:221
      GitHub personal access token detected: '...'
      → Rotate the credential immediately...

── 🟡 MEDIUM ──
  DUPLICATE-KEY  MEMORY.md:9
      Key 'Timezone' has conflicting values...
```

finding 按严重度分组,最严重的优先。每条 finding 显示 code、位置、message 和可执行的建议。`[fixable]` 标记表示 `--fix` 可修复的 finding。

### JSON(`--json`)

```json
{
  "workspace": "/home/vmser/.openclaw/workspace",
  "generated_at": "2026-06-05T13:50:33Z",
  "stats": { "memory_files_scanned": 5, "text_files_scanned": 48 },
  "summary": {
    "total": 3,
    "by_severity": {"critical": 1, "medium": 1, "info": 1},
    "by_code": {"SECRET-GHP": 1, "DUPLICATE-KEY": 1, "FILE-MISSING": 1},
    "worst": "critical"
  },
  "findings": [
    {"code": "SECRET-GHP", "severity": "critical", "path": "...", "line": 221, "message": "...", "suggestion": "...", "fixable": false}
  ]
}
```

`summary.worst` 是出现的最高严重度(或 `"none"`)。这是 CI 状态检查推荐的单一字段。

### Quiet(`--quiet`)

```text
⚠️  3 finding(s), worst=critical
```

一行。专为 cron、heartbeat、状态栏以及仅需 pass/fail 信号的 pre-commit 钩子设计。

## 5. 退出码策略

| Code | Meaning | When |
|---|---|---|
| 0 | clean | 无 finding |
| 1 | findings | 至少一条 finding,无 critical |
| 2 | secret leaked | 任意 `SECRET-*` finding |
| 3 | internal error | 扫描过程中出现未预期异常 |

退出码 `2` **保留**给密钥。这是一个刻意的选择:让 CI 门禁(或 cron 任务、pre-push 钩子)对凭据泄露触发与过时条目不同的告警。两者对应的运维响应截然不同,退出码在无需解析 JSON 的前提下区分了它们。

## 6. 扩展点

当前设计中,有三处可以在不 fork 的前提下扩展该工具:

1. **新的检查函数。** 每个检查的签名为 `_check_*(workspace, *args) -> list[Finding]`。新增一个函数,接入 `run_doctor`,并在上述表中添加 finding code。新 code 即成为抑制规则的稳定句柄。

2. **`--exclude` 排除子树。** 传入相对路径(可重复)以在文本文件扫描中跳过该目录。memory 类文件(`MEMORY.md`、`.learnings/*.md`、`memory/*.md`)**绝不**被跳过——该工具的工作就是审计它们。
3. **项目配置(`.memory-doctor.json`)。** 位于工作区根目录的可选 JSON。Schema:

   ```json
   {
     "stale_days": 60,
     "max_memory_lines": 200,
     "max_section_lines": 30,
     "exclude": ["scripts/tests"],
     "redact": true,
     "ignore_file": ".memory-doctorignore"
   }
   ```

   CLI 标志始终优先于文件。包含未知键、类型错误或非法 JSON 的文件会触发退出码 `3`(内部错误),并在消息中指出有问题的文件。文件缺失则静默处理。

3. **抑制文件(`.memory-doctorignore`)。** 采用 gitignore 风格的语法。每一条非注释、非空行是一条规则:

   ```
   code:CODE                       # 抑制该 code 的所有 finding
   path:RELATIVE_PATH              # 抑制该路径下的所有 finding(glob)
   code:CODE path:REL[:LINE[-LINE]]
                                   # 抑制 code 与路径(可选行号或行号区间)同时匹配的 finding
   ```

   该工具在工作区根目录加载该文件,将每条规则解析为 `(codes, path_glob, line_lo, line_hi)` 元组,按顺序应用,首条命中胜出。被抑制的 finding 仍以 `suppressed: true` 和 `suppress_reason` 字符串保留在报告中,但不计入最严重档位退出码(第 3 步将"清白但有抑制"对应到退出码 `4`)。`--ignore-file` 标志可覆盖默认文件名。

   示例见 `QUICKSTART.md`。

## 7. Stdlib only

该工具旨在以零摩擦 vendor 进任何 Python 项目。哪怕添加一个小依赖,也会带来安装成本、安全面和版本钉死的心智负担。我们需要的检查(正则、文件遍历、JSON 解析、dataclass)在 Python 3.2 起就全部位于标准库中。

若你发现自己想要一个第三方 HTML 渲染器、模糊匹配器或 TOML 解析器,那是一个信号——该检查正在做该工具不该做的事。把复杂度上游推到运行在该工具**之前**的转换步骤中。

## 8. 测试策略

19 个单元测试,全部隔离,合计运行时间约 2 秒:

| Bucket | Count | Approach |
|---|---|---|
| `STALE-ITEM` | 3 | 植入不同 `Last-Seen` 年龄的条目,断言阈值行为 |
| `DUPLICATE-KEY` | 2 | 植入冲突与一致两种重复,断言仅冲突触发 |
| `DANGLING-REF` | 3 | 植入指向缺失目标的图,运行 `--fix`,断言被移除 |
| `SECRET-LEAK` | 3 | 在 `tempfile.mkdtemp()` 中植入真实形态的 token(绝不放在被审计的树中) |
| `BUDGET` | 2 | 植入超长文件,断言 MEMORY 与按 section 两种检查都触发 |
| `FILE-MISSING` | 1 | 创建再删除一个核心文件 |
| Output formats | 3 | 断言 `--json` 合法、`--quiet` 行数、人类可读输出关键字 |
| `--exclude` | 2 | 断言测试 fixture 路径被跳过、memory 文件不被跳过 |

`SECRET-LEAK` 测试使用 `tempfile.mkdtemp()` 而非把 token 植入被审计的测试文件,因此**fixture 本身绝不会在生产环境对测试目录运行该工具时触发误报**。

运行方式:

```bash
python3 scripts/tests/test_memory_doctor.py
```

## 9. 运维笔记

### Pre-commit 集成

`scripts/hooks/post-commit` 展示了推荐的本地模式:通过 `git config core.hooksPath scripts/hooks` 注册钩子,钩子在每次提交后运行 `memory-doctor --scan --quiet --exclude scripts/tests`。该钩子永不阻塞提交;若有 finding 则打印,两种情况都以 0 退出。跳过单次提交:`SKIP_MEMORY_DOCTOR=1 git commit ...`。

### CI 门禁

推荐的 CI 集成只有一行:

```bash
python3 scripts/memory-doctor.py --scan --quiet --exclude scripts/tests
```

随后检查退出码:`2` 无条件失败构建,`1` 警告,`0` 通过。更丰富的集成可消费 `--json` 输出,并在 `summary.worst in {"critical", "high"}` 时失败。

### 周期运行

周级 cron 或 heartbeat 是 staleness 检查的合适节奏。日级过度(条目不会一夜之间过时);月级太慢(到注意到时已积攒了一年未分诊的 finding)。对典型智能体而言,7 天窗口是甜区。

## 10. 局限与已知缺口

- **无跨文件重复检测。** 刻意为之,见上文 C2。
- **图中无 `op: "delete"` 处理。** 已记录。
- **输出中无密钥脱敏。** 当发现密钥时,该工具会打印该行前 80 个字符。这是一个隐私权衡。脱敏输出更安全,可贴入聊天,但难以据此行动。建议把任何产生了 `SECRET-*` finding 的工具运行视为敏感内容,不要把输出贴到共享渠道。
- **无流式扫描。** 对拥有数千个文件的工作区,文本文件扫描每次都遍历整棵树。这对典型智能体工作区(几百个文件以内)尚可,但对 monorepo 级别的目录需要流式化。遇到这种情况,请提 issue。该设计通过 `_gather_files` 中的生成器为流式化预留了空间。
- **建议仅支持英文。** 人类可读输出是英文。`suggestion` 字段是 JSON 输出中的一个字符串,是需要本地化时的合适位置。

## 11. 许可证

MIT。详见仓库根目录的 `LICENSE`。
