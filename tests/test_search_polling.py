"""检索状态轻量轮询和分页结果保持权限及兼容性。"""

pytest_plugins = ("test_api",)


def test_search_status_can_omit_large_result_payload(client):
    """旧调用仍返回结果，显式轻量轮询只去掉正文与冻结目录。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.jobs.insert_one, {
        "id": "poll-search", "taskId": "task", "kind": "SEARCH", "status": "RUNNING",
        "progress": 21, "resultCount": 1, "files": [{"id": "file"}],
        "results": [{"fileId": "file", "offset": 0, "text": "needle"}],
    })
    normal = client.get("/api/v1/log-searches/poll-search").json()
    assert normal["results"][0]["text"] == "needle"
    response = client.get("/api/v1/log-searches/poll-search?includeResults=false")
    assert response.status_code == 200
    light = response.json()
    assert light["resultCount"] == 1 and light["progress"] == 21
    assert "results" not in light and "files" not in light
    results = client.get("/api/v1/log-searches/poll-search/results").json()
    assert results["status"] == "RUNNING" and results["total"] == 1
    assert results["items"] == normal["results"]
    assert client.get("/api/v1/log-searches/absent?includeResults=false").status_code == 404
