"""遍历当前 Git 历史，并复用提交消息规则检查每一条提交。"""

import subprocess

from check_commit_message import validate

revisions = subprocess.check_output(["git", "rev-list", "HEAD"], text=True).splitlines()
failures = []
for revision in revisions:
    message = subprocess.check_output(["git", "show", "-s", "--format=%B", revision], text=True)
    if errors := validate(message):
        failures.append(f"{revision[:12]}: " + "; ".join(errors))
if failures:
    raise SystemExit("\n".join(failures))
