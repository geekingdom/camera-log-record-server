"""仅在后端分发阶段解释节点资源地址规则，不改变 Worker 心跳和设备连接逻辑。"""

from ipaddress import ip_address, ip_network


def normalize_resource_networks(values):
    """校验并规范化多个单 IP 或 CIDR，去重且保持管理员输入顺序。"""
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("节点资源地址规则必须为最多128项的列表")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("节点资源地址不能为空")
        text = value.strip()
        try:
            normalized = str(ip_network(text, strict=False)) if "/" in text else str(ip_address(text))
        except ValueError as error:
            raise ValueError("节点资源地址必须为有效IP或CIDR网段") from error
        if normalized not in result:
            result.append(normalized)
    return result


def accepts_resource(node, resource_ip):
    """通用节点接收所有资源；专用节点缺规则或地址无效时拒绝，不能扩大准入。"""
    if node.get("isGeneralNode", True):
        return True
    try:
        address = ip_address(resource_ip)
    except (ValueError, TypeError):
        return False
    for value in node.get("resourceNetworks", []):
        try:
            network = ip_network(value, strict=False)
        except (ValueError, TypeError):
            continue
        if address.version == network.version and address in network:
            return True
    return False


def routing_config(config):
    """只复制平台管理的路由字段，节点自行上报的同名字段不具有授权作用。"""
    return {"isGeneralNode": config.get("isGeneralNode", True),
            "resourceNetworks": config.get("resourceNetworks", [])}
