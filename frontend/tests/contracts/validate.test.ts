import { describe, expect, it } from "vitest";
import { ContractError, readOptionalBoolean, readOptionalNumber } from "../../src/contracts/validate";

describe("readOptionalBoolean", () => {
  it("returns the boolean when present", () => {
    expect(readOptionalBoolean({ recovered: true }, "recovered", "path")).toBe(true);
    expect(readOptionalBoolean({ recovered: false }, "recovered", "path")).toBe(false);
  });

  it("returns null when absent or null", () => {
    expect(readOptionalBoolean({}, "recovered", "path")).toBeNull();
    expect(readOptionalBoolean({ recovered: null }, "recovered", "path")).toBeNull();
  });

  it("rejects a non-boolean value without echoing it", () => {
    expect(() => readOptionalBoolean({ recovered: "yes" }, "recovered", "path")).toThrow(ContractError);
  });
});

describe("readOptionalNumber", () => {
  it("returns the finite number when present", () => {
    expect(readOptionalNumber({ sample_count: 3 }, "sample_count")).toBe(3);
  });

  it("returns null when absent, non-numeric, or non-finite", () => {
    expect(readOptionalNumber({}, "sample_count")).toBeNull();
    expect(readOptionalNumber({ sample_count: "3" }, "sample_count")).toBeNull();
    expect(readOptionalNumber({ sample_count: Number.NaN }, "sample_count")).toBeNull();
    expect(readOptionalNumber({ sample_count: Number.POSITIVE_INFINITY }, "sample_count")).toBeNull();
  });
});
