// 实时订阅和开放接口示例共用协议选择，保留访问主机、端口及IPv6地址。
export function websocketUrl(path: string, address: Pick<Location, "protocol" | "host">): string {
  return `${address.protocol === "https:" ? "wss:" : "ws:"}//${address.host}${path}`;
}
