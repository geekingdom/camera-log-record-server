import { describe, expect, it } from "vitest";
import { shanghaiDateTimeRange, shanghaiDateTimeToUtc, shanghaiDayBounds, shanghaiToday } from "./archiveDate";

describe("归档日期边界", () => {
  it("按上海自然日生成检索起止时间", () => {
    expect(shanghaiDayBounds("2026-09-11")).toEqual({
      start: "2026-09-10T16:00:00.000Z",
      end: "2026-09-11T16:00:00.000Z",
    });
  });

  it("从任意 UTC 时刻得到对应的上海日期", () => {
    expect(shanghaiToday(new Date("2026-09-10T16:00:00.000Z"))).toBe("2026-09-11");
  });

  it("将分钟精度的上海本地时间转换为 UTC ISO，且不依赖浏览器本地时区", () => {
    expect(shanghaiDateTimeToUtc("2026-09-11T00:15")).toBe("2026-09-10T16:15:00.000Z");
    expect(shanghaiDateTimeToUtc("2026-09-11T23:45")).toBe("2026-09-11T15:45:00.000Z");
  });

  it("拒绝非分钟精度和不存在的上海日期时间", () => {
    expect(() => shanghaiDateTimeToUtc("2026-09-11T08:15:30")).toThrow(RangeError);
    expect(() => shanghaiDateTimeToUtc("2026-02-30T08:15")).toThrow(RangeError);
    expect(() => shanghaiDateTimeToUtc("")).toThrow(RangeError);
  });

  it("默认范围为最近一小时，并保持上海本地分钟文本", () => {
    expect(shanghaiDateTimeRange(new Date("2026-09-11T04:30:45.000Z"))).toEqual({
      start: "2026-09-11T11:30",
      end: "2026-09-11T12:30",
    });
  });
});
