# PR Skills — 本地化 PR 提交前置拦截系统

> 一套面向 [Trae IDE](https://www.trae.ai/) 的 AI Skill 集合，在 PR 提交前自动执行代码审查、安全扫描、测试评估、变更记录和提交格式校验。

> A collection of AI Skills for [Trae IDE](https://www.trae.ai/) that automate pre-PR checks — code review, security scanning, test coverage assessment, changelog generation, and commit message formatting.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 🎯 设计理念 / Design Philosophy

将原本臃肿的"一站式代码审查"流程，按单一职责原则拆解为 **5 个功能独立、互不重叠的 AI Skill**。每个 Skill 只专注一个维度，最大化 AI 专注度和准确率。

Instead of a monolithic code review, we decompose the workflow into **5 independent, non-overlapping AI Skills** following the Single Responsibility Principle. Each Skill focuses on one dimension, maximizing AI accuracy and focus.

---

## 🧩 Skills 概览 / Skills Overview

| # | Skill | 职责 / Responsibility | 输出 / Output |
|---|-------|----------------------|-------------|
| 1 | **Linting & Code Style Inspector** | 未使用变量、拼写错误、低级告警、最佳实践违背 | Findings + Remediation |
| 2 | **Changeset & Changelog Generator** | 变更分类、SemVer 判断、`.changeset` 草稿 | SemVer Bump + Changeset Draft |
| 3 | **Test & Codecov Assessor** | 测试覆盖分析、Codecov 影响预估、测试骨架 | Coverage Impact + Test Skeleton |
| 4 | **CodeQL & Security Scanner** | 注入漏洞、硬编码密钥、不安全 API、认证授权 | CWE/OWASP + Remediation Priority |
| 5 | **Commit Message Formatter** | Conventional Commits 校验、自动重写 | Validation + Rewritten Message |

---

## 📁 项目结构 / Project Structure

```
pr-skills/
├── .trae/skills/                         # Trae IDE 全局 Skill 注册
│   ├── 01-linting-code-style-inspector/
│   │   └── skill.md                      # ← 含 YAML frontmatter (name/description/trigger)
│   ├── 02-changeset-changelog-generator/
│   │   └── skill.md
│   ├── 03-test-codecov-assessor/
│   │   └── skill.md
│   ├── 04-codeql-security-scanner/
│   │   └── skill.md
│   └── 05-commit-message-formatter/
│       └── skill.md
├── skills/                               # 用户可读的 Skill 文档
│   ├── 01-linting-code-style-inspector/
│   │   └── skill.md
│   ├── 02-changeset-changelog-generator/
│   │   └── skill.md
│   ├── 03-test-codecov-assessor/
│   │   └── skill.md
│   ├── 04-codeql-security-scanner/
│   │   └── skill.md
│   └── 05-commit-message-formatter/
│       └── skill.md
└── README.md
```

> 两个目录的区别：`skills/` 存放纯 Markdown 格式的用户文档，`.trae/skills/` 额外包含 YAML frontmatter 元数据（`name`、`description`、`trigger`），供 Trae IDE 自动发现和注册。

> **Difference**: `skills/` holds user-facing Markdown docs; `.trae/skills/` includes YAML frontmatter metadata (`name`, `description`, `trigger`) for Trae IDE auto-discovery and registration.

---

## 🔍 各 Skill 详解 / Skill Details

### 1. Linting & Code Style Inspector（代码规范审查员）

| 属性 | 值 |
|------|-----|
| 注册名 | `linting-code-style-inspector` |
| 触发时机 | PR 中代码变更需要做规范审查时 |

**检查项**：
- 未使用的变量、函数、导入
- 标识符拼写错误与命名规范
- 编译器/标准 linter 低级告警（不可达代码、空指针风险等）
- 语言/框架最佳实践违背（`!!` 断言、`any` 滥用、未处理 Promise 等）
- 与项目现有代码的风格一致性

**Checks**:
- Unused variables, functions, imports
- Spelling errors and naming convention violations
- Compiler/linter warnings (unreachable code, null safety, etc.)
- Framework best practice violations (`!!`, `any`, unhandled Promise, etc.)
- Style consistency with existing codebase

---

### 2. Changeset & Changelog Generator（版本与变更记录员）

| 属性 | 值 |
|------|-----|
| 注册名 | `changeset-changelog-generator` |
| 触发时机 | PR 包含业务逻辑变更、API 变更、breaking change 或新功能时 |

**检查项**：
- 根据 diff 自动判定变更类别（major / minor / patch）
- 检查 `.changeset/` 文件是否缺失
- 推断 scope 并生成变更记录草稿
- 自动检测并标记 BREAKING CHANGE

**Checks**:
- Auto-classify changes (major / minor / patch) from diff
- Check for missing `.changeset` files
- Infer scope and generate changeset draft
- Auto-detect and flag BREAKING CHANGE

---

### 3. Test & Codecov Assessor（测试与覆盖率评估员）

| 属性 | 值 |
|------|-----|
| 注册名 | `test-codecov-assessor` |
| 触发时机 | PR 包含业务逻辑新增或修改时 |

**检查项**：
- 生产代码与测试文件的映射分析
- 关键路径（公开 API / 异常分支 / 核心业务逻辑）覆盖判定
- Codecov 影响预估（覆盖率变化百分比）
- 自动适配项目测试框架，生成测试骨架代码
- 每个函数至少建议 2 个边界/异常测试场景

**Checks**:
- Map production code to corresponding test files
- Critical path coverage (public API / error branches / core logic)
- Codecov impact estimation
- Auto-detect test framework and generate test skeletons
- At least 2 edge case test suggestions per function

---

### 4. CodeQL & Security Scanner（静态安全扫描员）

| 属性 | 值 |
|------|-----|
| 注册名 | `codeql-security-scanner` |
| 触发时机 | PR 涉及用户输入、数据库操作、认证授权、配置变更等敏感代码区域时 |

**检查项**：
- 注入漏洞：SQL/NoSQL/OS Command/XSS/路径穿越/SSRF
- 硬编码敏感信息：API Key / Token / 密码 / 内网地址 / 私钥
- 不安全 API：弱加密算法、禁用 TLS 验证、不安全反序列化、CORS 配置错误
- 认证授权缺陷：缺少鉴权中间件、IDOR、Token 泄露、弱哈希
- 依赖与 CI 工作流安全

**Checks**:
- Injection: SQL/NoSQL/OS Command/XSS/Path Traversal/SSRF
- Hardcoded secrets: API Key / Token / Password / Private Keys
- Insecure APIs: weak crypto, disabled TLS, unsafe deserialization, CORS misconfig
- Auth flaws: missing middleware, IDOR, token leaks, weak hashing
- Dependency and CI workflow security

---

### 5. Commit Message Formatter（提交规范校验员）

| 属性 | 值 |
|------|-----|
| 注册名 | `commit-message-formatter` |
| 触发时机 | PR 提交阶段（pre-commit 或 squash merge 前）|

**检查项**：
- Conventional Commits 格式校验（type / scope / description / body / footer）
- type 与 diff 内容对齐检测
- Breaking Change 检测与 footer 注入
- Scope 自动推断
- 多类型变更时的拆分建议
- Issue/PR 引用闭合检测（Closes / Fixes / Refs）

**Checks**:
- Conventional Commits format validation
- type-to-diff alignment check
- Breaking Change detection and footer injection
- Auto scope inference
- Split suggestion for mixed-type changes
- Issue/PR reference closure (Closes / Fixes / Refs)

---

## 🚀 安装与使用 / Installation & Usage

### 前提条件 / Prerequisites

- 已安装 [Trae IDE](https://www.trae.ai/)
- Git 仓库

### 安装 / Install

```bash
git clone https://github.com/BobcGn/pr-skills.git
```

将 `pr-skills/.trae/skills/` 目录复制到目标项目的 `.trae/skills/` 目录中：

Copy the `pr-skills/.trae/skills/` directory into your target project's `.trae/skills/` directory:

```bash
cp -r pr-skills/.trae/skills/* your-project/.trae/skills/
```

Trae IDE 会自动发现并注册这些 Skill。之后在对话中即可通过 `Skill` 工具调用。

Trae IDE will automatically discover and register these Skills. You can then invoke them via the `Skill` tool in conversations.

### 使用 / Usage

在 Trae IDE 中打开目标项目，在对话中说：

In Trae IDE, open your target project and say in the chat:

- "请对当前 PR 执行代码规范审查" / "Run linting check on this PR"
- "检查本次变更是否缺少 changeset" / "Check if this PR is missing a changeset"
- "评估本次变更的测试覆盖率" / "Assess test coverage for this PR"
- "执行安全扫描" / "Run a security scan"
- "校验我的 commit message" / "Validate my commit message"

也可以一次性串联所有 Skill，实现完整的 Pre-PR 检查流水线。

You can also chain all Skills together for a complete Pre-PR check pipeline.

---

## 🏗️ 架构设计 / Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Pre-PR Check Suite                    │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Linting  │  │Changeset │  │  Test &  │              │
│  │ & Style  │  │ & Change │  │ Codecov  │              │
│  │Inspector │  │ log Gen  │  │ Assessor │              │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘              │
│       │              │              │                    │
│       ▼              ▼              ▼                    │
│  ┌─────────────────────────────────────┐                │
│  │         DIFF 分析引擎                │                │
│  │      (共享的代码变更上下文)          │                │
│  └─────────────────────────────────────┘                │
│       ▲              ▲              ▲                    │
│       │              │              │                    │
│  ┌────┴─────┐  ┌────┴─────┐  ┌────┴─────┐              │
│  │ CodeQL & │  │ Commit   │  │  (可扩展) │              │
│  │ Security │  │ Message  │  │  Future   │              │
│  │ Scanner  │  │Formatter │  │  Skills   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

- **单一职责 / Single Responsibility**: 每个 Skill 只关心一个维度，互不侵入。
- **统一输出 / Unified Output**: 所有 Skill 使用 `Findings → Severity → File → Line → Remediation` 的标准化问题描述模式。
- **语言无关 / Language Agnostic**: 支持 Kotlin / Java / TypeScript / Python / JavaScript 等主流语言。
- **零配置 / Zero Config**: 放到 `.trae/skills/` 目录即用。

---

## 📋 输出标准 / Output Standard

所有 Skill 输出遵循统一模式 / All Skills follow a unified output pattern:

```
## [Skill Name] Report

### Summary
- 关键指标汇总 / Key metrics summary

---

### Finding #N: [Severity] Title
- **File**: path/to/file.ext
- **Line**: L123-L127
- **Code Snippet**: (有问题的代码 / problematic code)
- **Description**: (问题说明 / issue description)
- **Remediation**: (修复代码 / fix code)

---

### Remediation Priority
- [Critical] 必须合并前修复 / Must fix before merge
- [High] 建议合并前修复 / Recommended before merge
- [Medium] 可后续修复 / Can fix later
```

---

## 📄 许可证 / License

MIT

---

## 🤝 贡献 / Contributing

欢迎提交 Issue 和 Pull Request。添加新 Skill 时请遵循以下规范：

Issues and PRs are welcome. When adding new Skills, please follow these guidelines:

1. 在 `skills/` 和 `.trae/skills/` 中分别创建目录
2. `.trae/skills/` 中的 `skill.md` 必须包含 YAML frontmatter（`name`、`description`、`trigger`）
3. 输出格式遵循统一的 `Findings + Remediation` 模式
4. 新 Skill 的职责不得与已有 Skill 重叠

1. Create directories in both `skills/` and `.trae/skills/`
2. `.trae/skills/skill.md` must include YAML frontmatter (`name`, `description`, `trigger`)
3. Output must follow the unified `Findings + Remediation` pattern
4. New Skill must not overlap with existing Skill responsibilities
