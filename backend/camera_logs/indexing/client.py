"""有界 Elasticsearch HTTP 合同：逐项确认批量结果，拒绝分片失败的阴性证据。"""

import hashlib
import json

import httpx

CHUNK_BYTES = 32 * 1024
OVERLAP = 16
VERSION = 1


def fingerprint(file):
    """把来源身份、冻结水位与摘要绑定到不可混用的索引版本。"""
    fields = [VERSION, *(file.get(key) for key in
              ("id", "taskId", "nodeId", "runId", "sessionId", "bytes", "sha256"))]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def connection(settings):
    """凭据仅留在HTTP头，不在URL、异常日志或API返回中暴露。"""
    headers = {"Authorization": "ApiKey " + settings.elasticsearch_api_key} if settings.elasticsearch_api_key else {}
    return httpx.Client(base_url=settings.elasticsearch_url.rstrip("/"), headers=headers,
                        timeout=settings.elasticsearch_timeout_seconds, verify=settings.elasticsearch_verify_tls)


def checked(response):
    """成功HTTP仍可能含超时或分片失败，不能把这类响应解释为零命中。"""
    response.raise_for_status()
    data = response.json()
    if data.get("timed_out") or data.get("_shards", {}).get("failed", 0):
        raise ValueError("Elasticsearch查询不完整")
    return data


def ensure_index(client, index):
    """显式wildcard字段保留子串候选语义；正文副本不暴露给浏览器直连。"""
    response = client.put('/' + index, json={"settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {"dynamic": "strict", "properties": {
            **{key: {"type": "keyword"} for key in ("kind", "fileId", "taskId", "resourceId", "nodeId",
                "runId", "sessionId", "revision", "headHex", "tailHex")},
            "text": {"type": "wildcard"}, "offset": {"type": "long"}, "length": {"type": "long"},
            "bytes": {"type": "long"}, "chunks": {"type": "integer"}, "hasNewline": {"type": "boolean"},
            "receivedAt": {"type": "date"}, "indexedAt": {"type": "date"}, "hour": {"type": "date"},
        }}})
    if response.status_code == 400 and response.json().get("error", {}).get("type") == 'resource_already_exists_exception':
        return
    checked(response)


def bulk(client, index, documents):
    """任意单项失败都不推进本批检查点；稳定ID允许重放成功项而不重复。"""
    payload = ''.join(json.dumps(item, ensure_ascii=True) + '\n'
                      for identifier, doc in documents for item in ({"index": {"_id": identifier}}, doc))
    result = checked(client.post(f'/{index}/_bulk', content=payload,
                                 headers={"Content-Type": "application/x-ndjson"}))
    items = result.get("items", [])
    if len(items) != len(documents) or result.get("errors") or any(
        not 200 <= item.get("index", {}).get("status", 0) < 300 for item in items
    ):
        raise ValueError("Elasticsearch批量部分失败，检查点未推进")


def manifests(client, index, files):
    """按确定ID读取完成证明；缺失/变更/部分结果一律交回原文。"""
    result = checked(client.post(f'/{index}/_mget', json={"ids": ['manifest-' + fingerprint(f) for f in files]}))
    return {item['_source']['fileId']: item['_source'] for item in result.get('docs', [])
            if item.get('found') and item.get('_source', {}).get('kind') == 'manifest'}


def evidence(client, index, files, keyword=None):
    """匹配前三字符只作候选，最终完整行、接收时间与大小写仍由原文确认。

    每次验证分块总数与完成证明相符，防止外部删除、热数据到期或ES丢分片造成漏检。
    wildcard特殊字符转义；索引块保留16字节重叠，覆盖最多12字节的三字符UTF-8前缀。
    """
    proof = manifests(client, index, files)
    revisions = [fingerprint(f) for f in files if f['id'] in proof]
    if not revisions:
        return {}
    query = {"bool": {"filter": [{"term": {"kind": "chunk"}}, {"terms": {"revision": revisions}}]}}
    aggs = {"versions": {"terms": {"field": "revision", "size": len(revisions)}, "aggs": {}}}
    if keyword is not None:
        prefix = ''.join('\\' + c if c in '*?\\' else c for c in keyword[:3])
        aggs['versions']['aggs']['matches'] = {"filter": {"wildcard": {"text": {"value": '*' + prefix + '*'}}}}
    result = checked(client.post(f'/{index}/_search', json={"size": 0, "query": query, "aggs": aggs}))
    groups = result['aggregations']['versions']
    if groups.get('sum_other_doc_count', 0) or groups.get('doc_count_error_upper_bound', 0):
        raise ValueError("Elasticsearch覆盖统计不完整")
    counts = {item['key']: item for item in groups['buckets']}
    output = {}
    for file in files:
        rev, item = fingerprint(file), proof.get(file['id'])
        count = counts.get(rev, {})
        if (not item or item.get('revision') != rev or item.get('bytes') != file.get('bytes')
                or count.get('doc_count', 0) != item.get('chunks')):
            continue
        output[file['id']] = {"bytes": item['bytes'], "head": bytes.fromhex(item['headHex']),
            "tail": bytes.fromhex(item['tailHex']), "hasNewline": item['hasNewline'],
            "mayMatch": keyword is None or count.get('matches', {}).get('doc_count', 0) > 0}
    return output
