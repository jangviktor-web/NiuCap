#!/usr/bin/env bash
# 为什么要有这个钩子（防的是什么坑）：
# codegraph 索引不会自己更新——本仓库曾因此白瞎过：db 停在 10 月 8 日，
# 之后 44 个提交的新代码（新文件/新函数/新调用关系）全部查不到，
# 后面每次排查都得先手动 sync，忘了就是拿旧索引干活。
# post-commit 里挂一次增量 sync（实测 ~3s），commit 后索引自动保鲜。
#
# 设计约束（懒但不能坏事）：
#   - codegraph 不在 PATH（别的机器/新 clone）→ 静默跳过，不拦 commit
#   - sync 本身失败（db 锁/磁盘满）→ 吞掉，退出码恒 0，绝不让 hook 拦 commit
#   - -q 静音：hook 输出会混进 git 的 stdout，没必要刷屏
#
# 安装：cp scripts/codegraph-post-commit.sh .git/hooks/post-commit && chmod +x .git/hooks/post-commit
set -u

command -v codegraph >/dev/null 2>&1 || exit 0

# 仓库根目录执行，sync 才能找到 .codegraph/（hook 的 cwd 默认就是仓库根，双保险）
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$root" ] || exit 0

codegraph sync -q "$root" >/dev/null 2>&1 || true
exit 0
