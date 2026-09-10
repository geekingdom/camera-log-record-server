"""命令记录内容快照与当前定时配置的独立预算展示。"""


async def execution_history(repo, listing, task_id, page, page_size, command_id=None, kind=None):
    """有界分页记录并批量读取当前运行预算，不按正文合并不同配置。"""
    task = await repo.get("tasks", task_id)
    query = {"taskId": task_id}
    if command_id:
        query.update(commandId=command_id, kind="SCHEDULED")
    if kind:
        query["kind"] = kind
    result = await listing("commands", query, page, page_size)
    configured = {item["id"]: item for item in task.get("scheduledCommands", [])}
    run_id = task.get("runId")
    budget_ids = [f"{run_id}:{identifier}" for identifier in configured] if run_id else []
    budgets = {item["_id"]: item.get("attempts", 0) async for item in repo.db.budgets.find({"_id": {"$in": budget_ids}})}
    result["runId"] = run_id
    result["scheduledCommands"] = [
        {"id": identifier, "command": item.get("command"), "totalExecutions": item["totalExecutions"],
         "intervalSeconds": item.get("intervalSeconds"), "attempts": budgets.get(f"{run_id}:{identifier}", 0)}
        for identifier, item in configured.items()
    ]
    for execution in result["items"]:
        execution["commandSource"] = "SNAPSHOT" if execution.get("command") is not None else "UNAVAILABLE"
        # 任务编辑会重生定时配置 ID；只能用相同 ID 恢复未保存正文的历史行。
        # 已删除配置无法可靠重建，绝不按列表位置或相似命令猜测。
        if execution.get("command") is None and execution.get("kind") == "SCHEDULED":
            command = configured.get(execution.get("commandId"))
            if command and command.get("command"):
                execution["command"] = command["command"]
                execution["commandSource"] = "CURRENT_CONFIGURATION"
    return result
