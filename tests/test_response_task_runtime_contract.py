"""任务运行时公开投影必须由独立中文字段合同完整覆盖。"""

from camera_logs.common.api_contracts.response_identity import COMMAND, OPERATION
from camera_logs.common.api_contracts.response_task_runtime import TASK_RUNTIME_FIELDS
from camera_logs.common.api_contracts.response_tasks import INITIAL_COMMAND, SCHEDULED_COMMAND, TASK
from camera_logs.common.database import public


def _assert_documented(value, schema, path="task"):
    """递归检查真实公开状态夹具的每个字段都有中文合同。"""
    assert schema["description"], f"{path} 缺少中文说明"
    if isinstance(value, dict):
        for key, child in value.items():
            assert key in schema["properties"], f"{path}.{key} 缺少运行时字段合同"
            _assert_documented(child, schema["properties"][key], f"{path}.{key}")


def test_task_runtime_fields_document_public_recovery_and_health_fixture():
    """模拟持久化的恢复、健康、部署和从机状态，先经生产 public 投影再核对合同。"""
    task = {
        "id": "task", "status": "BLOCKED", "desiredState": "STOPPED", "nodeId": "node-old",
        "runId": "run-old", "sessionId": "session-old", "generation": 4, "storageIdentity": "camera-a",
        "error": "节点失联", "archiveError": "归档延迟", "updatedAt": "2026-10-20T12:00:00+08:00",
        "pausedAt": "2026-10-20T11:00:00+08:00", "restartRequested": False,
        "controlOperationId": "operation", "controlClaimVersion": 9, "commandClaimVersion": 4,
        "creationOperationId": "created",
        "operationId": "created", "creationOperationStatus": "PENDING",
        "resumeWaiting": {"operationId": "resume", "pausedRunId": "run-old", "generation": 4,
                          "requestedAt": "2026-10-20T10:00:00+08:00", "waitingReason": "OFFLINE",
                          "identityChangedAt": "2026-10-20T10:05:00+08:00"},
        "resourceHealthRecovery": {"resourceId": "resource", "stoppedAt": "2026-10-20T09:00:00+08:00",
                                   "desiredState": "RUNNING", "reason": "AUTH_FAILED",
                                   "observedAt": "2026-10-20T09:01:00+08:00", "authorizedAt": "2026-10-20T09:02:00+08:00"},
        "reconnectWake": {"resourceId": "resource", "requestedAt": "2026-10-20T09:03:00+08:00"},
        "closedReceipt": {"taskId": "task", "runId": "run-old", "generation": 4, "nodeId": "node-old",
                          "sessionId": "session-old", "instanceId": "worker-old", "closedAt": "2026-10-20T09:04:00+08:00",
                          "reason": "AUTO_FAILOVER_FENCED"},
        "isolationEvidence": "已关闭旧连接", "recoveryReason": "PLANNED_NODE_RESTART",
        "plannedRestart": True, "plannedRestartNodeId": "node-old", "plannedRestartToken": "restart-token",
        "failoverFenceRetryAt": "2026-10-20T09:05:00+08:00", "failoverConsumeRetryAt": "2026-10-20T09:06:00+08:00",
        "failoverExcludedNodeId": "node-old", "failoverFromNodeId": "node-old", "failoverReason": "AUTO_FAILOVER_FENCED",
        "failoverReleasedAt": "2026-10-20T09:07:00+08:00", "failoverOriginalHealthySince": "2026-10-20T09:08:00+08:00",
        "failoverOriginalHealthyObservedAt": "2026-10-20T09:09:00+08:00",
        "deploymentRecovery": {"id": "task", "taskId": "task", "runId": "run-old", "generation": 4, "nodeId": "node-old",
                               "sessionId": "session-old", "token": "deploy-token", "preparedAt": "2026-10-20T09:10:00+08:00",
                               "desiredState": "RUNNING", "controlClaimVersion": 9, "controlOperationId": "operation"},
        "deploymentClosedProof": {"token": "deploy-token", "nodeId": "node-old", "taskId": "task",
                                  "runId": "run-old", "generation": 4, "sessionId": "session-old",
                                  "confirmedAt": "2026-10-20T09:11:00+08:00"},
        "shellMode": "PSH", "debugPhase": "ALREADY_ASH", "commandBlocked": False, "debugError": "无",
        "effectiveSshPort": 18080, "slaveConnectedAt": "2026-10-20T09:12:00+08:00",
        "coredumpMountStatus": "MOUNTED", "coredumpMountError": None, "coredumpMountRunId": "run-old",
        "coredumpCheckedAt": "2026-10-20T09:13:00+08:00", "deletedAt": "2026-10-20T09:14:00+08:00",
        "deletionState": "PENDING", "deletionCompletedAt": "2026-10-20T09:15:00+08:00",
        "resourceDeleted": True, "resourceDeletedAt": "2026-10-20T09:16:00+08:00",
    }

    exposed = public(task)
    for key, value in exposed.items():
        if key in TASK_RUNTIME_FIELDS:
            _assert_documented(value, TASK_RUNTIME_FIELDS[key], key)
    assert set(TASK_RUNTIME_FIELDS) <= set(exposed)


def test_task_runtime_excludes_transient_resume_claim_credentials():
    """恢复领取令牌仅用于调度 CAS，不能随任务状态返回给 API 调用方。"""
    exposed = public({"id": "task", "resumeClaimToken": "internal-token", "resumeClaimExpires": "2026-10-20T12:00:00+08:00"})

    assert exposed == {"id": "task"}


def test_closed_receipt_identity_fields_allow_legacy_missing_owner_values():
    """关闭收据写入器直接读取可缺失的历史 generation 和 nodeId。"""
    receipt = TASK_RUNTIME_FIELDS["closedReceipt"]["properties"]
    assert receipt["generation"]["nullable"] is True
    assert receipt["nodeId"]["nullable"] is True


def test_task_and_command_contracts_describe_actual_send_and_control_boundaries():
    """目录说明必须区分实际写入后的等待、定时首轮间隔和历史字段缺失。"""
    initial = INITIAL_COMMAND["properties"]
    scheduled = SCHEDULED_COMMAND["properties"]
    task = TASK["properties"]

    assert "写入完成" in initial["delaySeconds"]["description"]
    assert "发送前" not in initial["delaySeconds"]["description"]
    assert "首次发送前" in scheduled["intervalSeconds"]["description"]
    assert "不会另行施加" in scheduled["delaySeconds"]["description"]
    assert scheduled["intervalSeconds"]["type"] == "integer"
    assert "TELNET_SERIAL" in task["protocol"]["description"]
    assert all(target in task["sshTarget"]["description"] for target in ("HOST", "SLAVE_1", "SLAVE_2", "SLAVE_3"))
    assert "固定为 PENDING" in TASK_RUNTIME_FIELDS["creationOperationStatus"]["description"]
    assert "需用 operationId 查询" in TASK_RUNTIME_FIELDS["creationOperationStatus"]["description"]
    assert "历史记录可能省略" in COMMAND["properties"]["status"]["description"]
    assert "历史操作可能省略" in OPERATION["properties"]["desiredState"]["description"]
