#!/usr/bin/env bash
# =============================================================================
# PR Skills Hook 安装脚本
# =============================================================================
# 用法:
#   cd /path/to/your/git/repo
#   bash /path/to/pr-skills/scripts/setup-hooks.sh
#
# 此脚本将:
#   1. 为当前仓库安装 pre-push hook (软链接到 pr-skills/scripts/pre-push.py)
#   2. 检查 LLM_API_KEY 环境变量是否已配置
#   3. 提供可选的 .env 文件配置方式
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRE_PUSH_SRC="${SCRIPT_DIR}/pre-push.py"
GIT_DIR="$(git rev-parse --git-dir 2>/dev/null || true)"
HOOKS_DIR="${GIT_DIR}/hooks"
PRE_PUSH_DST="${HOOKS_DIR}/pre-push"

# ANSI 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()  { echo -e "${CYAN}[Setup]${RESET} $1"; }
log_ok()    { echo -e "${GREEN}[Setup]${RESET} ✅ $1"; }
log_warn()  { echo -e "${YELLOW}[Setup]${RESET} ⚠️  $1"; }
log_err()   { echo -e "${RED}[Setup]${RESET} ❌ $1"; }

# ---- 检查是否在 Git 仓库中 ----
if [ -z "${GIT_DIR}" ]; then
    log_err "当前目录不在 Git 仓库中，请在 Git 仓库根目录运行此脚本"
    exit 1
fi

log_info "Git 仓库: $(git rev-parse --show-toplevel)"
log_info "Hook 目录: ${HOOKS_DIR}"

# ---- 检查 pre-push.py 是否存在 ----
if [ ! -f "${PRE_PUSH_SRC}" ]; then
    log_err "找不到 pre-push.py: ${PRE_PUSH_SRC}"
    exit 1
fi

# ---- 确保 pre-push.py 可执行 ----
chmod +x "${PRE_PUSH_SRC}"
log_info "已设置 pre-push.py 执行权限"

# ---- 创建 hooks 目录 ----
mkdir -p "${HOOKS_DIR}"

# ---- 备份已有 hook ----
if [ -f "${PRE_PUSH_DST}" ] || [ -L "${PRE_PUSH_DST}" ]; then
    BACKUP="${PRE_PUSH_DST}.backup.$(date +%Y%m%d%H%M%S)"
    mv "${PRE_PUSH_DST}" "${BACKUP}"
    log_warn "已备份现有 pre-push hook: ${BACKUP}"
fi

# ---- 创建软链接 ----
ln -sf "${PRE_PUSH_SRC}" "${PRE_PUSH_DST}"
log_ok "已安装 pre-push hook: ${PRE_PUSH_DST} -> ${PRE_PUSH_SRC}"

# ---- 检查环境变量 ----
echo ""
log_info "--- 环境变量检查 ---"

if [ -n "${LLM_API_KEY:-}" ]; then
    masked_key="${LLM_API_KEY:0:8}...${LLM_API_KEY: -4}"
    log_ok "LLM_API_KEY: ${masked_key}"
else
    log_warn "LLM_API_KEY 环境变量未设置"
    echo ""
    echo "  请选择以下方式之一进行配置:"
    echo ""
    echo "  方式 1: 在 shell 配置文件中设置 (推荐)"
    echo "    echo 'export LLM_API_KEY=\"sk-xxxxxxxx\"' >> ~/.zshrc"
    echo "    source ~/.zshrc"
    echo ""
    echo "  方式 2: 在项目的 .git/hooks/.env.pre-push 文件中设置"
    echo "    echo 'LLM_API_KEY=sk-xxxxxxxx' > ${HOOKS_DIR}/.env.pre-push"
    echo ""
    echo "  方式 3: 使用 direnv (.envrc)"
    echo "    echo 'export LLM_API_KEY=sk-xxxxxxxx' > .envrc"
    echo "    direnv allow"
fi

echo ""
log_ok "安装完成！"
log_info "下次执行 git push 时将自动触发 5 个 Skill 的代码审查"
log_info ""
log_info "如需跳过审查 (紧急情况):"
log_info "  git push --no-verify"
