// 资源指标图片导出：将已冻结的 ECharts 图像与业务元信息排版为独立 PNG。
export type MetricImageMetadata = {
  title: string;
  device: string;
  range: string;
};

function wrapLines(context: CanvasRenderingContext2D, text: string, maximumWidth: number) {
  const lines: string[] = [];
  let line = "";
  for (const character of text) {
    const candidate = `${line}${character}`;
    if (line && context.measureText(candidate).width > maximumWidth) {
      lines.push(line);
      line = character;
    } else line = candidate;
  }
  if (line) lines.push(line);
  return lines;
}

/** 依据图片实际宽度自动换行，保留图表的像素比例并在其上方加入可追溯元信息。 */
export async function metricChartPng(sourceUrl: string, metadata: MetricImageMetadata) {
  const source = new Image();
  source.src = sourceUrl;
  await source.decode();
  const scale = 2;
  const padding = 24 * scale;
  const titleLineHeight = 28 * scale;
  const metadataLineHeight = 20 * scale;
  const canvas = document.createElement("canvas");
  canvas.width = source.naturalWidth;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("浏览器不支持图片导出画布");
  const maximumWidth = canvas.width - padding * 2;
  context.font = `600 ${22 * scale}px sans-serif`;
  const titleLines = wrapLines(context, metadata.title, maximumWidth);
  context.font = `${13 * scale}px sans-serif`;
  const deviceLines = wrapLines(context, metadata.device, maximumWidth);
  const rangeLines = wrapLines(context, metadata.range, maximumWidth);
  const headerHeight = padding + titleLines.length * titleLineHeight + (deviceLines.length + rangeLines.length) * metadataLineHeight + 20 * scale;
  canvas.height = source.naturalHeight + headerHeight;
  const drawing = canvas.getContext("2d");
  if (!drawing) throw new Error("浏览器不支持图片导出画布");
  drawing.fillStyle = "#ffffff";
  drawing.fillRect(0, 0, canvas.width, canvas.height);
  let y = padding + 22 * scale;
  drawing.fillStyle = "#1f2933";
  drawing.font = `600 ${22 * scale}px sans-serif`;
  for (const line of titleLines) { drawing.fillText(line, padding, y); y += titleLineHeight; }
  y += 4 * scale;
  drawing.fillStyle = "#52616b";
  drawing.font = `${13 * scale}px sans-serif`;
  for (const line of [...deviceLines, ...rangeLines]) { drawing.fillText(line, padding, y); y += metadataLineHeight; }
  drawing.drawImage(source, 0, headerHeight);
  return canvas.toDataURL("image/png");
}
