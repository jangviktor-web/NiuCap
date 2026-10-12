#!/usr/bin/env bash
# 为什么要有这个钩子（防的是什么坑）：
# data/ 里躺着用户数据库（账号密码哈希、会话 token、持仓、成交、自选股），
# .env 里躺着明文数据库密码 —— 这些绝不能出现在任何一次推送里。
# .gitignore 是第一道防线，但「git add -f 强推」或新人误操作仍可能绕过，
# 所以推之前再过一道闸：命中敏感路径直接拒绝（exit 1），一个字节都出不去。
#
# 安装：cp scripts/pre-push-hook.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
# 误拦处理：人工确认文件确实无害后，git push --no-verify 强推（不建议）。
set -u

# 命中这些规则的文件不允许出现在任何一次推送里：
#   data/                  用户数据库、缓存、运行状态（tick.db 有 9GB，强推必炸）
#   backups/ backup_*/     本地备份，可能含整库快照
#   .env                   明文数据库密码
#   *.db *.db-wal *.db-shm *.sqlite*  *.log  *.bak
PATHS='^(data/|backups/|backup_[^/]*/)'
EXTS='\.(db|db-wal|db-shm|sqlite|sqlite3|log|bak)$'
DENY='^(data/|backups/|backup_[^/]*/|\.env$)'

fail=0
deny_hit() {
    echo "pre-push: 拒绝推送 —— '$1' 命中敏感路径规则" >&2
    fail=1
}

# pre-push 的 stdin 每行: <local_ref> <local_sha> <remote_ref> <remote_sha>
# 本地 sha 全 0 表示删除远端分支（没有内容要推，跳过）
while read -r local_ref local_sha remote_ref remote_sha; do
    [ -z "${local_sha:-}" ] && continue
    case "$local_sha" in *[!0-9a-f]*|"") continue ;; esac
    [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
    # 远端 sha 全 0 表示这是个新分支，跟空树比
    if [ -z "${remote_sha:-}" ] || [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
        base=$(git hash-object -t tree /dev/null)
    else
        base="$remote_sha"
    fi
    # 只看本次推送新增/修改/重命名的文件（A/C/M/R/T），删除不管
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        case "$f" in
            .env) deny_hit "$f" ;;
        esac
        if printf '%s\n' "$f" | grep -qE "$PATHS|$EXTS"; then
            deny_hit "$f"
        fi
    done < <(git diff --name-only --diff-filter=ACMRT "$base" "$local_sha" 2>/dev/null)
done

# 双保险：仓库里已跟踪的文件本来就不该有敏感文件（防历史里误提交后一直带着）
while IFS= read -r f; do
    [ -z "$f" ] && continue
    deny_hit "已跟踪: $f"
done < <(git ls-files | grep -E "$DENY|$EXTS" | grep -v '^\.env\.example$')

if [ "$fail" -eq 1 ]; then
    echo "pre-push: 已拦截。确认无害可 git push --no-verify 强推（不推荐）。" >&2
    exit 1
fi
exit 0
