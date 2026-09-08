"""任务领取身份条件：所有旧实例回写必须受运行、节点与代次约束。"""


def owner_filter(task):
    """匹配领取时的不可变身份；缺省代次只匹配缺省值，不能匹配新代次。"""
    return {"id": task["id"], "runId": task["runId"],
            "nodeId": task.get("nodeId"), "generation": task.get("generation")}
