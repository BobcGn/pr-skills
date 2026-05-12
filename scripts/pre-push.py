#!/usr/bin/env python3
"""
Git pre-push hook: Automated LLM-powered code review with 5 concurrent skill checks.

触发时机: 每次执行 `git push` 时自动运行
安装方法:
  1. 将此脚本复制或软链接到 .git/hooks/pre-push:
     ln -sf ../../scripts/pre-push.py .git/hooks/pre-push
  2. 确保脚本有执行权限:
     chmod +x .git/hooks/pre-push
  3. 设置必需的环境变量:
     export LLM_API_KEY="sk-xxxxxxxx"

行为说明:
  - 自动提取本次 push 的所有 diff 和 commit messages
  - 并发调用 LLM API 执行 5 个代码审查 Skill (Linting / Changesets / Test / Security / Commit)
  - 若任一 Skill 返回拦截项(🛑)，阻断 push (exit 1)
  - 全部通过或有警告但无拦截，放行 push (exit 0)
  - API 超时或网络错误时自动降级放行，打印警告
"""

import os
import sys
import json
import subprocess
import re
import time
import tempfile
import textwrap
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

# ============================================================================
# 配置
# ============================================================================
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")

# LLM 后端使用 IDE 内置模型，以下为硬编码默认值
# 若需切换，请直接修改下方常量
LLM_BASE_URL = "https://api.openai.com/v1"
LLM_MODEL = "gpt-4o"
LLM_TIMEOUT = 30

# 要执行的 Skill 列表
SKILLS = [
    "linting",
    "changeset",
    "test-coverage",
    "security",
    "commit-message",
]

# ============================================================================
# ANSI 终端颜色
# ============================================================================
class Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

# 线程安全打印锁
_print_lock = Lock()


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class SkillResult:
    """单个 Skill 的执行结果，包含状态与原始 LLM 响应。"""
    skill_name: str
    status: str
    response: Optional[str] = None


@dataclass
class BlockerEntry:
    """从 Skill 响应中提取的拦截项详情。"""
    skill_name: str
    display_name: str
    severity: str
    file_path: str
    line_range: str
    description: str
    fix_suggestion: str


@dataclass
class RemediationAction:
    """
    自动修复动作。

    action_type:
        "code_patch"   - 通过 git apply 应用 diff 补丁
        "write_file"   - 写入文件（如 .changeset/xxx.md 或测试骨架）
        "git_amend"    - 执行 git commit --amend
        "manual"       - 仅提示，需手动修复
    """
    action_type: str
    skill_name: str
    display_name: str
    description: str
    fix_content: str
    target_file: str = ""
    command: str = ""

def log_info(msg: str) -> None:
    with _print_lock:
        print(f"{Colors.CYAN}[PR-Skills]{Colors.RESET} {msg}", flush=True)

def log_pass(msg: str) -> None:
    with _print_lock:
        print(f"{Colors.GREEN}[PR-Skills] ✅ {msg}{Colors.RESET}", flush=True)

def log_warn(msg: str) -> None:
    with _print_lock:
        print(f"{Colors.YELLOW}[PR-Skills] ⚠️  {msg}{Colors.RESET}", flush=True)

def log_block(msg: str) -> None:
    with _print_lock:
        print(f"{Colors.RED}[PR-Skills] 🛑 {msg}{Colors.RESET}", flush=True)

def log_skill_header(name: str) -> None:
    with _print_lock:
        print(f"\n{Colors.BOLD}{Colors.BLUE}━━━ {name} ━━━{Colors.RESET}", flush=True)


# ============================================================================
# Git 上下文提取
# ============================================================================

def get_push_info() -> list[tuple[str, str, str, str]]:
    """
    从 stdin 读取 pre-push hook 传入的 ref 更新信息。

    pre-push hook 标准输入格式（每行）:
        <local ref> <local sha1> <remote ref> <remote sha1>

    Returns:
        list of (local_ref, local_sha, remote_ref, remote_sha)
    """
    lines = []
    for line in sys.stdin:
        line = line.strip()
        if line:
            parts = line.split()
            if len(parts) >= 4:
                lines.append((parts[0], parts[1], parts[2], parts[3]))
    return lines


def run_git(args: list[str]) -> str:
    """执行 git 命令并返回标准输出。"""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log_warn(f"Git 命令超时: git {' '.join(args)}")
        return ""
    except FileNotFoundError:
        log_block("未找到 git 命令，请确认 git 已安装并在 PATH 中")
        sys.exit(1)


def get_diff_and_commits(push_refs: list[tuple[str, str, str, str]]) -> tuple[str, str]:
    """
    根据 push 的 ref 信息提取完整的 diff 和 commit messages。

    Args:
        push_refs: pre-push hook 传入的 ref 更新列表

    Returns:
        (combined_diff, combined_commits)
    """
    all_diffs: list[str] = []
    all_commits: list[str] = []

    for local_ref, local_sha, remote_ref, remote_sha in push_refs:
        zero_sha = "0" * 40
        force_zero_short = "0000000000000000000000000000000000000000"

        is_new_branch = (remote_sha in (zero_sha, force_zero_short))
        is_deleted = (local_sha in (zero_sha, force_zero_short))

        if is_deleted:
            continue

        if is_new_branch:
            diff = run_git(["diff", local_sha, "--", "."])
            commits = run_git(["log", local_sha, "--not", "--remotes", "--format=%h %s", "--", "."])
        else:
            diff = run_git(["diff", f"{remote_sha}..{local_sha}", "--", "."])
            commits = run_git(["log", f"{remote_sha}..{local_sha}", "--format=%h %s", "--", "."])

        if diff:
            all_diffs.append(diff)
        if commits:
            all_commits.append(commits)

    combined_diff = "\n\n".join(all_diffs) if all_diffs else "(no code changes detected)"
    combined_commits = "\n".join(all_commits) if all_commits else "(no commits detected)"

    return combined_diff, combined_commits


# ============================================================================
# Skill System Prompts
# ============================================================================

def build_system_prompt(skill_name: str) -> str:
    """
    构建指定 Skill 的完整 System Prompt。

    每个 Skill 的 Prompt 包含 Role、Objective、Rules & Checklist、Output Format，
    并在末尾添加统一的结果标记指令，便于脚本解析拦截/告警/通过状态。
    """
    prompts = {
        # ================================================================
        # Skill 1: Linting & Code Style Inspector
        # ================================================================
        "linting": """\
# Role: Linting & Code Style Inspector（代码规范审查员）

## Objective
严格审查本次 diff 中的所有代码变更，识别未使用的变量/导入、拼写错误、语言层面的低级告警以及违反语言/框架最佳实践的写法，输出结构化的 Findings 和 Remediation。

## Rules & Checklist
1. **未使用实体检测**：扫描新增或修改的变量、函数、导入、类型定义，标记声明后从未被引用的实体。
2. **拼写与命名规范**：检查新增标识符是否存在拼写错误，校验命名是否符合项目约定（camelCase/PascalCase/snake_case/UPPER_SNAKE_CASE）。
3. **语言级别低级告警**：冗余分号、不可达代码、空指针风险、未关闭的资源等。
4. **最佳实践违背**：如 Kotlin 中 `!!` 强制非空断言、Java 中 `System.out.println`、JS/TS 中 `any` 滥用等。
5. **代码一致性**：缩进、括号风格、import 组织方式是否与项目一致。

## Output Format
使用以下结构，按严重度（Error/Warning/Info）分类：

### Finding #N: [严重度] 标题
- **严重度**: Error | Warning | Info
- **文件**: path/to/file.ext
- **行号**: Lxx-Lyy
- **问题描述**: ...
- **修复建议**: ...

## 结果标记指令 (IMPORTANT)
在报告的**末尾**，你必须输出一个独立行作为结果标记，格式如下：
- 若存在 Error 级别问题 → `[RESULT] 🛑 BLOCKED (N errors, M warnings, K info)`
- 若仅有 Warning/Info 级别问题 → `[RESULT] ⚠️ WARNING (N warnings, M info)`
- 若没有任何问题 → `[RESULT] ✅ PASS`

请基于 git diff 内容做出独立判断，不要猜测或臆造不存在的问题。""",

        # ================================================================
        # Skill 2: Changeset & Changelog Generator
        # ================================================================
        "changeset": """\
# Role: Changeset & Changelog Generator（版本与变更记录员）

## Objective
分析本次 PR 的 diff，理解业务逻辑变更的本质（功能、修复、破坏性变更等），检查是否需要 changeset/changelog 条目，基于 diff 自动分类 SemVer 级别并生成变更记录草稿。

## Rules & Checklist
1. **变更类别自动判定**：
   - `major`：移除公开 API、修改函数签名导致编译失败、数据库 schema 不兼容变更。
   - `minor`：新增公开 API（新函数/类/模块）、新增可选参数且向后兼容。
   - `patch`：Bug 修复、性能优化、文档更新、内部重构。
2. **Changeset 检查**：检查 `.changeset/` 目录是否有对应的变更记录文件。
3. **Scope 自动推断**：根据文件路径推断 scope。
4. **Breaking Change 标记**：若存在 breaking change，必须添加 `BREAKING CHANGE:` 描述。
5. **多语言项目适配**：检测项目约定格式。

## Output Format
### Change Category Analysis
| 文件 | 变更类型 | 分类 | 说明 |

### Changeset Draft (若缺失)
```markdown
---
"@scope/package-name": major/minor/patch
---
```

## 结果标记指令 (IMPORTANT)
在报告的**末尾**，你必须输出一个独立行作为结果标记，格式如下：
- 若检测到 missing changeset 或 breaking change 未标记 → `[RESULT] 🛑 BLOCKED (reason)`
- 若 changeset 存在但有格式问题 → `[RESULT] ⚠️ WARNING (reason)`
- 若 changeset 完整且正确，或无需 changeset → `[RESULT] ✅ PASS`

请基于 git diff 内容做出独立判断，不要猜测或臆造不存在的问题。""",

        # ================================================================
        # Skill 3: Test & Codecov Assessor
        # ================================================================
        "test-coverage": """\
# Role: Test & Codecov Assessor（测试与覆盖率评估员）

## Objective
分析本次 diff 中所有新增或修改的生产代码，评估其是否被现有/新增的单元测试覆盖；对未覆盖的关键路径预估 Codecov 影响，生成测试骨架代码。

## Rules & Checklist
1. **变更-测试映射分析**：对每个生产代码文件，查找对应测试文件。
   - `src/main/.../Foo.kt` → `src/test/.../FooTest.kt`
   - 若测试文件不存在 → `missing_test_file`
   - 若存在但未新增针对本次变更的测试用例 → `missing_test_case`
2. **覆盖关键路径判定**：
   - 公开 API 和异常/错误处理分支：**必须**有测试覆盖
   - 核心业务逻辑：**必须**有测试覆盖
   - 内部私有辅助函数：建议有测试，非强制
3. **Codecov 影响预估**：
   - 若预估覆盖率下降超过 1%，标记为高风险
4. **测试框架适配**：自动检测项目测试框架，生成骨架代码。
5. **边界条件测试建议**：每个函数至少建议 2 个边界/异常场景。

## Output Format
### Coverage Impact Analysis
| 生产文件 | 测试文件 | 状态 | 新增行数 | 已覆盖行数 | 缺口 |

### Test Skeleton for: [文件名]
```language
// 测试骨架代码
```

## 结果标记指令 (IMPORTANT)
在报告的**末尾**，你必须输出一个独立行作为结果标记，格式如下：
- 若存在 missing_test_file 或核心路径 missing_test_case（如公开 API 无测试） → `[RESULT] 🛑 BLOCKED (reason)`
- 若仅有非核心路径缺少测试用例，或有测试文件但不完善 → `[RESULT] ⚠️ WARNING (reason)`
- 若覆盖完整 → `[RESULT] ✅ PASS`

请基于 git diff 内容做出独立判断，不要猜测或臆造不存在的问题。""",

        # ================================================================
        # Skill 4: CodeQL & Security Scanner
        # ================================================================
        "security": """\
# Role: CodeQL & Security Scanner（静态安全扫描员）

## Objective
以安全审计视角逐行审查本次 diff，识别一切可能被利用的安全漏洞——从注入攻击到信息泄露，输出 OWASP Top 10 对标的风险清单和具体的修复代码。

## Rules & Checklist
1. **注入类漏洞扫描**：SQL/NoSQL 注入、OS 命令注入、XSS、路径穿越、SSRF。
2. **硬编码敏感信息检测**：API Key/Token/Secret/Password 字面量、数据库连接字符串密码、内网地址、私钥。
3. **不安全 API 调用与配置**：弱加密算法（MD5/SHA1/DES/RC4）、TLS 绕过、不安全反序列化、CORS 错误配置。
4. **认证与授权漏洞**：缺少鉴权中间件、IDOR、Token 泄露（localStorage）、弱哈希。
5. **依赖与工作流安全**：新增依赖的已知漏洞、CI 脚本注入、Dockerfile root 运行。

## Output Format
### Vulnerability #N: [严重度] 标题
- **严重度**: 🔴 Critical / 🟠 High / 🟡 Medium / 🔵 Low
- **CWE**: CWE-xxx
- **OWASP Category**: Axx:2021 – xxx
- **文件**: path/to/file.ext
- **行号**: Lxx-Lyy
- **风险说明**: ...
- **修复代码**: ...

## 结果标记指令 (IMPORTANT)
在报告的**末尾**，你必须输出一个独立行作为结果标记，格式如下：
- 若存在 Critical 或 High 级别漏洞 → `[RESULT] 🛑 BLOCKED (N critical, M high, K medium, L low)`
- 若仅有 Medium 或 Low 级别漏洞 → `[RESULT] ⚠️ WARNING (N medium, M low)`
- 若没有任何安全漏洞 → `[RESULT] ✅ PASS`

请基于 git diff 内容做出独立判断，不要猜测或臆造不存在的问题。""",

        # ================================================================
        # Skill 5: Commit Message Formatter
        # ================================================================
        "commit-message": """\
# Role: Commit Message Formatter（提交规范校验员）

## Objective
严格依据 Conventional Commits 1.0.0 规范（兼容 Angular Commit Convention）校验 commit message；检测格式错误、类型范围缺失、描述不规范等问题，并基于 diff 生成符合规范的 Rewritten Message。

## Rules & Checklist
1. **格式强制性校验**：
   ```
   <type>(<scope>): <short description>
   <空行>
   <optional body>
   <空行>
   <optional footer(s)>
   ```
   - `type` 必须为: feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert
   - `short description` 以英文小写动词开头，不超过 72 字符，不以句号结尾
2. **类型与 Diff 对齐校验**：根据 diff 推断正确 type，与当前 message 对比。
3. **Breaking Change 检测与 Footer 注入**：若 diff 包含 breaking change，footer 必须标注。
4. **Scope 自动推断**：根据文件路径推断 scope。
5. **Ref 闭合检测**：检查 footer 中的 Issue/PR 引用。

## Output Format
### Validation Result: ❌ Invalid / ✅ Valid
#### Issues Found
| # | 问题类型 | 严重度 | 说明 |

### Rewritten Commit Message
```
<rewritten message>
```

## 结果标记指令 (IMPORTANT)
在报告的**末尾**，你必须输出一个独立行作为结果标记，格式如下：
- 若 commit message 存在 Error 级别问题（如缺少 breaking change 标记、type 完全错误）→ `[RESULT] 🛑 BLOCKED (reason)`
- 若仅有 Warning 级别问题（如描述过长、缺少 scope、首字母大写） → `[RESULT] ⚠️ WARNING (reason)`
- 若 commit message 完全合规 → `[RESULT] ✅ PASS`

请基于 git diff 和 commit message 内容做出独立判断，不要猜测或臆造不存在的问题。""",
    }

    return prompts.get(skill_name, "")


# ============================================================================
# LLM API 调用
# ============================================================================

def call_llm(system_prompt: str, user_prompt: str, timeout: int = LLM_TIMEOUT) -> Optional[str]:
    """
    通过 OpenAI 兼容 API 调用 LLM。

    Args:
        system_prompt: System Prompt（Skill 定义）
        user_prompt: User Prompt（包含 git diff 和 commit messages）
        timeout: 超时秒数

    Returns:
        LLM 返回的文本内容；失败或超时时返回 None
    """
    if not LLM_API_KEY:
        log_warn("LLM_API_KEY 环境变量未设置，跳过 LLM 调用")
        return None

    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
            choice = result.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "")
            return content
    except Exception as e:
        log_warn(f"LLM API 调用失败: {e}")
        return None


# ============================================================================
# 结果解析
# ============================================================================

def parse_result(content: Optional[str]) -> str:
    """
    解析 LLM 返回内容，提取结果标记。

    Args:
        content: LLM 返回的完整文本

    Returns:
        "blocked" / "warning" / "pass" / "unknown"
    """
    if content is None:
        return "unknown"

    blocker_pattern = re.compile(r"\[RESULT\]\s*🛑\s*BLOCKED", re.IGNORECASE)
    warning_pattern = re.compile(r"\[RESULT\]\s*⚠️\s*WARNING", re.IGNORECASE)
    pass_pattern = re.compile(r"\[RESULT\]\s*✅\s*PASS", re.IGNORECASE)

    if blocker_pattern.search(content):
        return "blocked"
    elif warning_pattern.search(content):
        return "warning"
    elif pass_pattern.search(content):
        return "pass"
    else:
        return "unknown"


def extract_result_line(content: Optional[str]) -> str:
    """从 LLM 返回内容中提取 [RESULT] 行。"""
    if content is None:
        return "API 调用失败（无返回内容）"

    for line in content.split("\n"):
        if re.search(r"\[RESULT\]", line, re.IGNORECASE):
            return line.strip()

    return "未能解析结果标记（content length: {}）".format(len(content))


# ============================================================================
# Skill 并发编排
# ============================================================================

SKILL_DISPLAY_NAMES = {
    "linting":         "01. Linting & Code Style Inspector (代码规范审查)",
    "changeset":       "02. Changeset & Changelog Generator (版本变更记录)",
    "test-coverage":   "03. Test & Codecov Assessor (测试与覆盖率评估)",
    "security":        "04. CodeQL & Security Scanner (静态安全扫描)",
    "commit-message":  "05. Commit Message Formatter (提交规范校验)",
}

SKILL_ICONS = {
    "linting":         "📝",
    "changeset":       "📋",
    "test-coverage":   "🧪",
    "security":        "🔒",
    "commit-message":  "✍️",
}


def run_skill_check(skill_name: str, diff: str, commits: str) -> tuple[str, str, Optional[str]]:
    """
    执行单个 Skill 的 LLM 检查。

    Args:
        skill_name: Skill 标识名
        diff: 完整的 git diff
        commits: commit messages

    Returns:
        (skill_name, result_status, llm_response)
    """
    system_prompt = build_system_prompt(skill_name)
    user_prompt = f"""Please review the following git push changes and provide your analysis.

## Commit Messages
```
{commits}
```

## Git Diff
```diff
{diff}
```

Please follow the output format specified in your system instructions and remember to include the [RESULT] marker at the very end."""

    icon = SKILL_ICONS.get(skill_name, "")
    display = SKILL_DISPLAY_NAMES.get(skill_name, skill_name)
    log_skill_header(f"{icon} {display}")

    start = time.time()
    response = call_llm(system_prompt, user_prompt)
    elapsed = time.time() - start

    result_status = parse_result(response)
    result_line = extract_result_line(response)

    status_colors = {
        "blocked": f"{Colors.RED}🛑 BLOCKED{Colors.RESET}",
        "warning": f"{Colors.YELLOW}⚠️  WARNING{Colors.RESET}",
        "pass":    f"{Colors.GREEN}✅ PASS{Colors.RESET}",
        "unknown": f"{Colors.YELLOW}⚠️  DEGRADED{Colors.RESET}",
    }

    status_display = status_colors.get(result_status, status_colors["unknown"])

    log_info(f"  耗时: {elapsed:.1f}s | 结果: {status_display}")
    log_info(f"  判定: {result_line}")

    if response and response.strip():
        lines = response.strip().split("\n")
        if len(lines) > 20:
            log_info(f"  --- 完整报告 (前 50 行) ---")
            for line in lines[:50]:
                with _print_lock:
                    print(f"  {Colors.CYAN}│{Colors.RESET} {line}", flush=True)
            log_info(f"  ... (共 {len(lines)} 行, 仅展示前 50 行)")
        else:
            log_info(f"  --- 完整报告 ---")
            for line in lines:
                with _print_lock:
                    print(f"  {Colors.CYAN}│{Colors.RESET} {line}", flush=True)

    return (skill_name, result_status, response)


def run_all_skills(diff: str, commits: str) -> dict[str, SkillResult]:
    """
    并发执行所有 Skill 检查。

    Args:
        diff: 完整的 git diff
        commits: commit messages

    Returns:
        {skill_name: SkillResult}  包含原始 LLM 响应，供后续失败分析使用
    """
    results: dict[str, SkillResult] = {}

    max_workers = len(SKILLS)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(run_skill_check, skill, diff, commits): skill
            for skill in SKILLS
        }

        for future in as_completed(future_map):
            skill = future_map[future]
            try:
                skill_name, status, response = future.result()
                results[skill_name] = SkillResult(
                    skill_name=skill_name,
                    status=status,
                    response=response,
                )
            except Exception as e:
                log_warn(f"Skill '{skill}' 执行异常: {e}")
                results[skill] = SkillResult(
                    skill_name=skill,
                    status="unknown",
                    response=None,
                )

    return results


# ============================================================================
# 阻断/放行逻辑
# ============================================================================

def evaluate_results(results: dict[str, SkillResult]) -> bool:
    """
    评估所有 Skill 结果，决定是放行还是阻断。

    Args:
        results: {skill_name: SkillResult}

    Returns:
        True = 放行 (exit 0), False = 阻断 (exit 1)
    """
    blocked_skills: list[str] = []
    warning_skills: list[str] = []
    passed_skills: list[str] = []
    unknown_skills: list[str] = []

    for skill_name, sr in results.items():
        display = SKILL_DISPLAY_NAMES.get(skill_name, skill_name)
        if sr.status == "blocked":
            blocked_skills.append(display)
        elif sr.status == "warning":
            warning_skills.append(display)
        elif sr.status == "pass":
            passed_skills.append(display)
        else:
            unknown_skills.append(display)

    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  PR Skills 审查结果汇总{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

    if blocked_skills:
        log_block(f"发现 {len(blocked_skills)} 个拦截项:")
        for s in blocked_skills:
            print(f"     🛑 {s}")
        print()

    if warning_skills:
        log_warn(f"发现 {len(warning_skills)} 个警告项:")
        for s in warning_skills:
            print(f"     ⚠️  {s}")
        print()

    if passed_skills:
        log_pass(f"通过 {len(passed_skills)} 项检查:")
        for s in passed_skills:
            print(f"     ✅ {s}")
        print()

    if unknown_skills:
        log_warn(f"{len(unknown_skills)} 项检查降级处理（API 异常或超时，已自动放行）:")
        for s in unknown_skills:
            print(f"     ⚠️  {s}")
        print()

    if blocked_skills:
        log_block("=" * 60)
        log_block("  Push 已被阻断！请修复上述拦截项后重新提交。")
        log_block("=" * 60)
        return False
    else:
        log_pass("=" * 60)
        log_pass("  审查通过，Push 已放行。")
        log_pass("=" * 60)
        return True


# ============================================================================
# 失败报告与修复模块 (Failure Reporting & Remediation Module)
# ============================================================================

def extract_blockers_from_response(skill_name: str, response: str) -> list[BlockerEntry]:
    """
    从 LLM 响应中解析出所有 Error 级别的拦截项详情。

    根据 Skill 类型采用不同的解析策略：
    - linting / security: 解析 Finding/Vulnerability 条目
    - changeset: 检测 missing changeset / breaking change
    - test-coverage: 检测 missing_test_file / missing_test_case
    - commit-message: 检测 Issues Found 表格

    Args:
        skill_name: Skill 标识名
        response: LLM 原始响应文本

    Returns:
        拦截项列表
    """
    entries: list[BlockerEntry] = []
    display = SKILL_DISPLAY_NAMES.get(skill_name, skill_name)

    if skill_name in ("linting", "security"):
        entries = _parse_structured_findings(skill_name, display, response)
    elif skill_name == "changeset":
        entries = _parse_changeset_blockers(skill_name, display, response)
    elif skill_name == "test-coverage":
        entries = _parse_test_blockers(skill_name, display, response)
    elif skill_name == "commit-message":
        entries = _parse_commit_blockers(skill_name, display, response)

    return entries


def _parse_structured_findings(skill_name: str, display: str, response: str) -> list[BlockerEntry]:
    """
    解析 linting/security 的结构化 Finding 条目。

    匹配模式:
        ### Finding #N: [Error|Critical|High] ...
        - **严重度**: Error|🔴 Critical|🟠 High
        - **文件**: path/to/file.ext
        - **行号**: Lxx-Lyy
        - **问题描述**: ...
        - **修复建议**: ...
    """
    entries: list[BlockerEntry] = []

    finding_pattern = re.compile(
        r"###\s+(?:Finding|Vulnerability)\s+#\d+:\s*\[([^\]]+)\]\s*(.+?)(?=###\s+(?:Finding|Vulnerability)|$)",
        re.DOTALL | re.IGNORECASE,
    )

    for match in finding_pattern.finditer(response):
        severity_raw = match.group(1).strip()
        body = match.group(2)

        if skill_name == "linting":
            if "error" not in severity_raw.lower():
                continue
        elif skill_name == "security":
            if not any(kw in severity_raw.lower() for kw in ("critical", "high", "🔴", "🟠")):
                continue

        file_path = _re_first(r"(?:文件|File)\s*[:：]\s*(.+?)(?:\n|$)", body)
        line_range = _re_first(r"(?:行号|Line)\s*[:：]\s*(.+?)(?:\n|$)", body)
        description = _re_first(r"(?:问题描述|风险说明|Description)\s*[:：]\s*(.+?)(?:\n|$)", body)
        fix_suggestion = _re_first(r"(?:修复建议|修复代码|Remediation|Fix)\s*[:：]\s*(.+?)(?:\n|$)", body)

        if not description:
            description = body[:120].strip()

        entries.append(BlockerEntry(
            skill_name=skill_name,
            display_name=display,
            severity=severity_raw,
            file_path=file_path or "(无法识别)",
            line_range=line_range or "N/A",
            description=description,
            fix_suggestion=fix_suggestion or "参见上方完整报告",
        ))

    if not entries:
        entries.append(_fallback_entry(skill_name, display, response, "存在 Error 级拦截项"))

    return entries


def _parse_changeset_blockers(skill_name: str, display: str, response: str) -> list[BlockerEntry]:
    """解析 changeset Skill 的拦截项。"""
    entries: list[BlockerEntry] = []

    missing = re.search(r"missing.?changeset", response, re.IGNORECASE)
    breaking = re.search(r"breaking.?change.*未标记|not.?marked.*breaking", response, re.IGNORECASE)

    if missing:
        entries.append(BlockerEntry(
            skill_name=skill_name, display_name=display,
            severity="Error",
            file_path=".changeset/",
            line_range="N/A",
            description="缺少 changeset 文件，未记录本次变更",
            fix_suggestion="生成 changeset 草稿并写入 .changeset/ 目录",
        ))
    if breaking:
        entries.append(BlockerEntry(
            skill_name=skill_name, display_name=display,
            severity="Error",
            file_path="N/A",
            line_range="N/A",
            description="存在 Breaking Change 但未在 changeset 或 commit 中标注",
            fix_suggestion="添加 BREAKING CHANGE: 描述",
        ))
    if not entries:
        entries.append(_fallback_entry(skill_name, display, response, "changeset 配置异常"))

    return entries


def _parse_test_blockers(skill_name: str, display: str, response: str) -> list[BlockerEntry]:
    """解析 test-coverage Skill 的拦截项。"""
    entries: list[BlockerEntry] = []

    missing_file_match = re.findall(
        r"\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*missing_test_file\s*\|",
        response, re.IGNORECASE,
    )
    for prod_file, test_file in missing_file_match:
        entries.append(BlockerEntry(
            skill_name=skill_name, display_name=display,
            severity="Error",
            file_path=prod_file.strip(),
            line_range="N/A",
            description=f"生产文件缺少对应测试文件: {test_file.strip()}",
            fix_suggestion="创建测试文件并补充核心路径测试用例",
        ))

    missing_case_match = re.findall(
        r"\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*missing_test_case\s*\|",
        response, re.IGNORECASE,
    )
    for prod_file, test_file in missing_case_match:
        entries.append(BlockerEntry(
            skill_name=skill_name, display_name=display,
            severity="Error",
            file_path=prod_file.strip(),
            line_range="N/A",
            description=f"测试文件 {test_file.strip()} 存在但未覆盖本次变更的核心路径",
            fix_suggestion="在测试文件中补充新的测试用例",
        ))

    if not entries:
        entries.append(_fallback_entry(skill_name, display, response, "核心路径缺少测试覆盖"))

    return entries


def _parse_commit_blockers(skill_name: str, display: str, response: str) -> list[BlockerEntry]:
    """解析 commit-message Skill 的拦截项。"""
    entries: list[BlockerEntry] = []

    issue_rows = re.findall(
        r"\|\s*(\d+)\s*\|\s*([^|]+)\s*\|\s*Error\s*\|\s*(.+?)\s*\|",
        response, re.IGNORECASE,
    )
    for num, issue_type, desc in issue_rows:
        entries.append(BlockerEntry(
            skill_name=skill_name, display_name=display,
            severity="Error",
            file_path="commit message",
            line_range="N/A",
            description=f"[{issue_type.strip()}] {desc.strip()}",
            fix_suggestion="重写 commit message 为规范格式",
        ))

    if not entries:
        entries.append(_fallback_entry(skill_name, display, response, "commit message 格式不符合规范"))

    return entries


def _re_first(pattern: str, text: str) -> str:
    """正则提取第一个捕获组，失败返回空字符串。"""
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else ""


def _fallback_entry(skill_name: str, display: str, response: str, reason: str) -> BlockerEntry:
    """当结构化解析失败时，生成兜底的拦截项。"""
    return BlockerEntry(
        skill_name=skill_name,
        display_name=display,
        severity="Error",
        file_path="(详见下方报告)",
        line_range="N/A",
        description=reason,
        fix_suggestion=response[:300] + "..." if len(response) > 300 else response,
    )


# ---------------------------------------------------------------------------
# 诊断报告生成
# ---------------------------------------------------------------------------

def generate_diagnostic_report(
    blocked_results: dict[str, SkillResult],
    all_blockers: list[BlockerEntry],
    diff: str,
    commits: str,
) -> str:
    """
    生成标准化的 Markdown 诊断报告并写入 .git/pre_push_report.md。

    Args:
        blocked_results: 被拦截的 Skill 结果
        all_blockers: 所有解析出的拦截项
        diff: git diff 文本
        commits: commit messages

    Returns:
        报告文件路径
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = []

    lines.append(f"# PR Skills 预检报告")
    lines.append(f"")
    lines.append(f"> 生成时间: {now}")
    lines.append(f"> 拦截 Skill 数量: {len(blocked_results)}")
    lines.append(f"> 拦截项总数: {len(all_blockers)}")
    lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 拦截项详情")
    lines.append(f"")

    for i, b in enumerate(all_blockers, 1):
        lines.append(f"### 拦截项 #{i}: [{b.severity}] {b.display_name}")
        lines.append(f"")
        lines.append(f"| 属性 | 值 |")
        lines.append(f"|------|-----|")
        lines.append(f"| Skill | {b.display_name} |")
        lines.append(f"| 严重度 | {b.severity} |")
        lines.append(f"| 文件 | `{b.file_path}` |")
        lines.append(f"| 行号 | {b.line_range} |")
        lines.append(f"| 说明 | {b.description} |")
        lines.append(f"| 修复建议 | {b.fix_suggestion} |")
        lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## Skill 原始报告")
    lines.append(f"")

    for skill_name, sr in blocked_results.items():
        display = SKILL_DISPLAY_NAMES.get(skill_name, skill_name)
        lines.append(f"### {display}")
        lines.append(f"")
        lines.append(f"```")
        if sr.response:
            lines.append(sr.response[:6000])
            if len(sr.response) > 6000:
                lines.append(f"\n... (截断，完整长度 {len(sr.response)} 字符)")
        else:
            lines.append("(无响应)")
        lines.append(f"```")
        lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 上下文")
    lines.append(f"")
    lines.append(f"### Commit Messages")
    lines.append(f"```")
    lines.append(commits[:2000])
    lines.append(f"```")
    lines.append(f"")
    lines.append(f"### Git Diff (截取前 3000 字符)")
    lines.append(f"```diff")
    lines.append(diff[:3000])
    if len(diff) > 3000:
        lines.append(f"\n... (截断，完整长度 {len(diff)} 字符)")
    lines.append(f"```")
    lines.append(f"")

    report_content = "\n".join(lines)
    report_path = os.path.join(os.getcwd(), ".git", "pre_push_report.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return report_path


# ---------------------------------------------------------------------------
# 自动修复策略
# ---------------------------------------------------------------------------

def call_llm_for_patch(skill_name: str, blocker_entries: list[BlockerEntry], diff: str) -> Optional[str]:
    """
    请求 LLM 为 linting/security 拦截项生成 unified diff 补丁。

    Args:
        skill_name: 触发拦截的 Skill 名
        blocker_entries: 拦截项列表
        diff: 原始 git diff

    Returns:
        unified diff 格式的补丁文本；失败返回 None
    """
    display = SKILL_DISPLAY_NAMES.get(skill_name, skill_name)

    blocker_text = "\n".join(
        f"- [{b.severity}] {b.file_path}:{b.line_range} - {b.description}"
        for b in blocker_entries
    )

    system_prompt = textwrap.dedent(f"""\
    # Role: Auto-Remediation Code Fixer

    ## Objective
    针对 {display} 发现的代码问题进行自动修复，输出可直接应用的 unified diff 格式补丁。

    ## Rules
    1. 只修复被标记为 Error 级的问题
    2. 输出标准 unified diff 格式，可直接通过 `git apply` 应用
    3. 每个文件的修改作为独立的 diff 块
    4. 不要修改未被标记问题的代码
    5. 修复方式要符合项目原有编码风格

    ## Output Format
    仅输出 diff 内容，不要包含任何解释性文字:
    ```diff
    diff --git a/path/to/file b/path/to/file
    index xxxxxx..yyyyyy 100644
    --- a/path/to/file
    +++ b/path/to/file
    @@ -line,count +line,count @@
    -old code
    +new code
    ```""")

    user_prompt = textwrap.dedent(f"""\
    ## 拦截项
    {blocker_text}

    ## 原始 Git Diff
    ```diff
    {diff[:4000]}
    ```

    请生成可直接 git apply 的 unified diff 补丁。""")

    log_info(f"  正在请求 LLM 为 {display} 生成修复补丁...")
    response = call_llm(system_prompt, user_prompt)

    if response:
        diff_match = re.search(r"```(?:diff)?\s*\n(.*?)```", response, re.DOTALL)
        if diff_match:
            return diff_match.group(1).strip()
        return response.strip()
    return None


def extract_rewritten_commit_message(response: str) -> Optional[str]:
    """从 commit-message Skill 响应中提取重写后的 message。"""
    match = re.search(
        r"(?:Rewritten Commit Message|重写后的)\s*[:：]?\s*```\s*\n?(.+?)```",
        response, re.DOTALL | re.IGNORECASE,
    )
    if match:
        lines = match.group(1).strip().split("\n")
        return lines[0].strip() if lines else None
    return None


def extract_changeset_draft(response: str) -> Optional[str]:
    """从 changeset Skill 响应中提取 changeset 草稿。"""
    match = re.search(
        r"```(?:markdown|md)?\s*\n(---\n.*?\n---)\s*```",
        response, re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return None


def extract_test_skeleton(response: str) -> Optional[tuple[str, str]]:
    """
    从 test-coverage Skill 响应中提取测试骨架代码。

    Returns:
        (test_file_name, test_code) 或 None
    """
    block_match = re.search(
        r"(?:Test Skeleton for|测试骨架)\s*[:：]\s*(.+?)\s*```\w*\s*\n(.+?)```",
        response, re.DOTALL | re.IGNORECASE,
    )
    if block_match:
        return (block_match.group(1).strip(), block_match.group(2).strip())

    code_match = re.search(
        r"```(?:kotlin|java|typescript|javascript|python)\s*\n(.+?)```",
        response, re.DOTALL,
    )
    if code_match:
        return ("test_skeleton", code_match.group(1).strip())

    return None


def classify_remediation_actions(
    blocked_results: dict[str, SkillResult],
    all_blockers: list[BlockerEntry],
    diff: str,
) -> list[RemediationAction]:
    """
    根据拦截的 Skill 类型分类生成自动修复动作。

    Args:
        blocked_results: 被拦截的 Skill 结果
        all_blockers: 所有解析出的拦截项
        diff: 原始 git diff

    Returns:
        修复动作列表
    """
    actions: list[RemediationAction] = []

    for skill_name in ("commit-message", "changeset", "test-coverage", "linting", "security"):
        if skill_name not in blocked_results:
            continue

        sr = blocked_results[skill_name]
        display = SKILL_DISPLAY_NAMES.get(skill_name, skill_name)
        skill_blockers = [b for b in all_blockers if b.skill_name == skill_name]

        if skill_name == "commit-message" and sr.response:
            rewritten = extract_rewritten_commit_message(sr.response)
            if rewritten:
                actions.append(RemediationAction(
                    action_type="git_amend",
                    skill_name=skill_name,
                    display_name=display,
                    description=f"使用规范 commit message 重写: {rewritten[:80]}",
                    fix_content=rewritten,
                    command=f'git commit --amend -m "{rewritten}"',
                ))
            else:
                for b in skill_blockers:
                    actions.append(RemediationAction(
                        action_type="manual",
                        skill_name=skill_name,
                        display_name=display,
                        description=f"手动修复 commit message: {b.description}",
                        fix_content="请根据 Conventional Commits 规范重写: type(scope): description",
                    ))

        elif skill_name == "changeset" and sr.response:
            draft = extract_changeset_draft(sr.response)
            if draft:
                scope_match = re.search(r'"@[^/]+/([^"]+)"', draft)
                scope = scope_match.group(1) if scope_match else "auto-fix"
                safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", scope)
                changeset_file = f".changeset/{safe_name}.md"
                full_content = draft + f"\n"
                for b in skill_blockers:
                    full_content += f"\n- {b.description}"
                actions.append(RemediationAction(
                    action_type="write_file",
                    skill_name=skill_name,
                    display_name=display,
                    description=f"写入 changeset 文件: {changeset_file}",
                    fix_content=full_content,
                    target_file=changeset_file,
                ))
            else:
                for b in skill_blockers:
                    actions.append(RemediationAction(
                        action_type="manual",
                        skill_name=skill_name,
                        display_name=display,
                        description=f"手动创建 changeset: {b.description}",
                        fix_content="请使用 npx changeset 或手动创建 .changeset/ 目录下的文件",
                    ))

        elif skill_name == "test-coverage" and sr.response:
            skeleton = extract_test_skeleton(sr.response)
            if skeleton:
                test_file, test_code = skeleton
                actions.append(RemediationAction(
                    action_type="write_file",
                    skill_name=skill_name,
                    display_name=display,
                    description=f"写入测试骨架到: {test_file}",
                    fix_content=test_code,
                    target_file=test_file,
                ))
            else:
                for b in skill_blockers:
                    actions.append(RemediationAction(
                        action_type="manual",
                        skill_name=skill_name,
                        display_name=display,
                        description=f"手动补充测试: {b.file_path} - {b.description}",
                        fix_content=f"请为 {b.file_path} 创建或补充测试用例",
                    ))

        elif skill_name in ("linting", "security") and skill_blockers:
            patch = call_llm_for_patch(skill_name, skill_blockers, diff)
            if patch:
                actions.append(RemediationAction(
                    action_type="code_patch",
                    skill_name=skill_name,
                    display_name=display,
                    description=f"应用 {display} 修复补丁",
                    fix_content=patch,
                    target_file=f"/tmp/pr_skills_fix_{skill_name}.patch",
                ))
            else:
                for b in skill_blockers:
                    actions.append(RemediationAction(
                        action_type="manual",
                        skill_name=skill_name,
                        display_name=display,
                        description=f"手动修复: {b.file_path} - {b.description}",
                        fix_content=b.fix_suggestion,
                    ))

    return actions


# ---------------------------------------------------------------------------
# 交互式修复引导
# ---------------------------------------------------------------------------

def _is_tty() -> bool:
    """检测是否在交互式终端中运行（非 CI/管道）。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def print_remediation_plan(actions: list[RemediationAction]) -> None:
    """在控制台打印修复计划。"""
    auto_actions = [a for a in actions if a.action_type != "manual"]
    manual_actions = [a for a in actions if a.action_type == "manual"]

    if auto_actions:
        print(f"\n{Colors.BOLD}{Colors.CYAN}{'─' * 60}{Colors.RESET}")
        print(f"{Colors.BOLD}  可自动修复项 ({len(auto_actions)} 个){Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.CYAN}{'─' * 60}{Colors.RESET}")
        for a in auto_actions:
            action_labels = {
                "code_patch": f"{Colors.GREEN}📄 代码补丁{Colors.RESET}",
                "write_file": f"{Colors.GREEN}📝 写入文件{Colors.RESET}",
                "git_amend":  f"{Colors.GREEN}🔧 Git Amend{Colors.RESET}",
            }
            label = action_labels.get(a.action_type, a.action_type)
            print(f"  {label}  [{a.display_name}]")
            print(f"     {a.description}")
            print()

    if manual_actions:
        print(f"{Colors.YELLOW}  需手动修复项 ({len(manual_actions)} 个):{Colors.RESET}")
        for a in manual_actions:
            print(f"     ⚠️  [{a.display_name}] {a.description}")
        print()


def apply_code_patch(action: RemediationAction) -> bool:
    """应用 code_patch 类型的修复。"""
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        )
        tmp.write(action.fix_content)
        tmp.close()

        result = subprocess.run(
            ["git", "apply", "--check", tmp.name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log_warn(f"补丁检查失败，跳过: {result.stderr[:200]}")
            os.unlink(tmp.name)
            return False

        result = subprocess.run(
            ["git", "apply", tmp.name],
            capture_output=True, text=True, timeout=10,
        )
        os.unlink(tmp.name)
        if result.returncode == 0:
            log_pass(f"已应用补丁: {action.display_name}")
            return True
        else:
            log_warn(f"补丁应用失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        log_warn(f"应用补丁异常: {e}")
        return False


def apply_write_file(action: RemediationAction) -> bool:
    """写入文件类型的修复（changeset / 测试骨架）。"""
    try:
        target = os.path.join(os.getcwd(), action.target_file)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(action.fix_content)
        log_pass(f"已写入文件: {action.target_file}")
        return True
    except Exception as e:
        log_warn(f"写入文件失败 ({action.target_file}): {e}")
        return False


def apply_git_amend(action: RemediationAction) -> bool:
    """执行 git commit --amend 修复。"""
    try:
        escaped_msg = action.fix_content.replace("'", "'\\''")
        result = subprocess.run(
            ["git", "commit", "--amend", "-m", escaped_msg],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log_pass(f"已修正 commit message: {action.fix_content[:60]}...")
            return True
        else:
            log_warn(f"amend 失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        log_warn(f"amend 异常: {e}")
        return False


def apply_all_auto_fixes(actions: list[RemediationAction]) -> tuple[int, int]:
    """
    按顺序应用所有可自动修复的动作。

    Returns:
        (成功数, 失败数)
    """
    success, failed = 0, 0
    for action in actions:
        if action.action_type == "manual":
            continue
        log_info(f"正在应用: [{action.display_name}] {action.description[:60]}...")
        if action.action_type == "code_patch":
            ok = apply_code_patch(action)
        elif action.action_type == "write_file":
            ok = apply_write_file(action)
        elif action.action_type == "git_amend":
            ok = apply_git_amend(action)
        else:
            ok = False
        if ok:
            success += 1
        else:
            failed += 1
    return success, failed


def interactive_remediation_flow(
    actions: list[RemediationAction],
) -> bool:
    """
    交互式修复引导：向用户展示修复计划并询问是否应用。

    Args:
        actions: 修复动作列表

    Returns:
        True = 用户已应用修复（可重试 push）
        False = 用户拒绝或非交互环境
    """
    auto_actions = [a for a in actions if a.action_type != "manual"]

    if not auto_actions:
        log_info("没有可自动修复的拦截项，请手动处理后重新 push")
        return False

    print_remediation_plan(actions)

    if not _is_tty():
        log_info("检测到非交互式终端（CI 环境），跳过交互式修复引导")
        log_info("修复补丁内容已包含在 .git/pre_push_report.md 报告中，请手动处理")
        return False

    try:
        choice = input(
            f"{Colors.BOLD}{Colors.CYAN}"
            f"→ 发现 {len(auto_actions)} 个可自动修复项，是否立即应用修复？ [Y/n] "
            f"{Colors.RESET}"
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        log_warn("\n输入中断，跳过自动修复")
        return False

    if choice and choice != "y":
        log_info("已跳过自动修复，请手动处理后重新 push")
        return False

    print()
    log_info("正在应用自动修复...")
    print()

    success, failed = apply_all_auto_fixes(actions)

    print()
    if success > 0:
        log_pass(f"成功应用 {success} 项修复")
    if failed > 0:
        log_warn(f"{failed} 项修复失败，需手动处理")

    if success > 0:
        print()
        log_info("修复已应用，请执行以下步骤完成提交:")
        print(f"  {Colors.CYAN}1. 检查变更: git diff{Colors.RESET}")
        print(f"  {Colors.CYAN}2. 暂存修复: git add -A{Colors.RESET}")
        if any(a.action_type == "git_amend" for a in actions):
            print(f"  {Colors.CYAN}   （commit message 已通过 --amend 自动修正）{Colors.RESET}")
        else:
            print(f"  {Colors.CYAN}3. 提交修复: git commit -m \"fix: apply PR Skills auto-fixes\"{Colors.RESET}")
        print(f"  {Colors.CYAN}4. 重新推送: git push{Colors.RESET}")
        return True

    return False


# ---------------------------------------------------------------------------
# 失败流程入口
# ---------------------------------------------------------------------------

def handle_blocked_push(
    results: dict[str, SkillResult],
    diff: str,
    commits: str,
) -> None:
    """
    当预检失败时触发的完整失败处理流程。

    流程:
        1. 提取所有拦截项详情
        2. 生成 .git/pre_push_report.md 诊断报告
        3. 分类生成自动修复方案
        4. 交互式询问是否应用修复
        5. 输出引导信息
    """
    blocked_results = {
        name: sr for name, sr in results.items() if sr.status == "blocked"
    }

    log_info("正在分析拦截项并生成诊断报告...")

    all_blockers: list[BlockerEntry] = []
    for skill_name, sr in blocked_results.items():
        if sr.response:
            blockers = extract_blockers_from_response(skill_name, sr.response)
            all_blockers.extend(blockers)

    report_path = generate_diagnostic_report(blocked_results, all_blockers, diff, commits)
    log_info(f"诊断报告已保存: {Colors.BOLD}{report_path}{Colors.RESET}")

    print()
    for i, b in enumerate(all_blockers, 1):
        log_block(f"#{i} [{b.display_name}] {b.file_path}: {b.description[:100]}")

    print()
    actions = classify_remediation_actions(blocked_results, all_blockers, diff)

    interactive_remediation_flow(actions)


# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    """pre-push hook 主入口函数。"""
    log_info("PR Skills pre-push hook 已启动")

    if not LLM_API_KEY:
        log_warn("LLM_API_KEY 环境变量未设置，将跳过所有 LLM 检查并放行推送")
        log_warn("请在 shell 配置文件中设置: export LLM_API_KEY=\"sk-xxxxxxxx\"")
        return 0

    push_refs = get_push_info()
    if not push_refs:
        log_info("未检测到推送信息，放行")
        return 0

    diff, commits = get_diff_and_commits(push_refs)

    if diff == "(no code changes detected)":
        log_info("未检测到代码变更，跳过审查")
        return 0

    log_info(f"提取到 diff ({len(diff)} 字符) 和 commit messages ({len(commits)} 字符)")
    log_info(f"开始并发执行 {len(SKILLS)} 个 Skill 检查...")

    start = time.time()
    results = run_all_skills(diff, commits)
    total_elapsed = time.time() - start

    log_info(f"所有 Skill 检查完成，总耗时: {total_elapsed:.1f}s")

    should_pass = evaluate_results(results)

    if not should_pass:
        handle_blocked_push(results, diff, commits)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
