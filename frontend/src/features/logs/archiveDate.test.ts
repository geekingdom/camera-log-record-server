import { describe, expect, it } from "vitest";
import { shanghaiDayBounds, shanghaiToday } from "./archiveDate";

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
});
