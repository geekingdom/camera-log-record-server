/** 实时日志的正常关闭只表示服务端已确认任务终止；其它关闭仍按可恢复链路处理。 */
export function shouldReconnectLiveSocket(code: number, localTransportFailure = false) {
  return localTransportFailure || code !== 1000;
}
