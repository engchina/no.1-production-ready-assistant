import { describe, expect, it } from "vitest";

import { isValidTimezone } from "./timezones";

describe("isValidTimezone", () => {
  it.each(["UTC", "Asia/Tokyo", "America/New_York", "+00:00", "+09:00", "+05:30", "-03:30", "+23:59", ""])("%s を受け付ける", (value) => {
    expect(isValidTimezone(value)).toBe(true);
  });
  it.each(["Invalid/Zone", "+24:00", "+09:60", "+9:00", "09:00"])("%s を拒否する", (value) => {
    expect(isValidTimezone(value)).toBe(false);
  });
});
