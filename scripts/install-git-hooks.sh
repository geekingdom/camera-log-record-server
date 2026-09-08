#!/bin/sh
set -eu
# 将版本库内的钩子和提交模板注册到当前开发者环境。
git config core.hooksPath .githooks
git config commit.template .gitmessage
chmod +x .githooks/commit-msg
echo "已启用中文提交信息钩子与模板。"
