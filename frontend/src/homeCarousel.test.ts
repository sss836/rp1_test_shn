import { describe, expect, it } from "vitest";
import {
  caseSlot,
  circularIndex,
  initialCaseIndex,
  shouldAutoAdvance,
  uniquePartScopes
} from "./homeCarousel";

const cases = [
  { code: "TC-01" },
  { code: "TC-02" },
  { code: "TC-03" },
  { code: "TC-04" }
];

describe("home Test Case carousel", () => {
  it("wraps previous and next indices", () => {
    expect(circularIndex(0, -1, cases.length)).toBe(3);
    expect(circularIndex(3, 1, cases.length)).toBe(0);
    expect(caseSlot(3, 0, cases.length)).toBe("previous");
    expect(caseSlot(1, 0, cases.length)).toBe("next");
  });

  it("selects the current case again when the object changes", () => {
    expect(initialCaseIndex(cases, "TC-03")).toBe(2);
    expect(initialCaseIndex(cases, "TC-01")).toBe(0);
  });

  it("falls back to the first case without a matching current code", () => {
    expect(initialCaseIndex(cases, null)).toBe(0);
    expect(initialCaseIndex(cases, "TC-MISSING")).toBe(0);
  });

  it("never enables autoplay under reduced motion", () => {
    expect(shouldAutoAdvance({
      itemCount: cases.length,
      paused: false,
      focusPaused: false,
      hoverPaused: false,
      reducedMotion: true
    })).toBe(false);
    expect(shouldAutoAdvance({
      itemCount: cases.length,
      paused: false,
      focusPaused: false,
      hoverPaused: false,
      reducedMotion: false
    })).toBe(true);
  });

  it("renders one selector for each part scope", () => {
    const scopes = uniquePartScopes([
      { asset_kind: "MODULE", target_part_code: "SLEG", label: "first leg" },
      { asset_kind: "MODULE", target_part_code: "SLEG", label: "second leg" },
      { asset_kind: "MODULE", target_part_code: "SARM", label: "arm" }
    ]);
    expect(scopes.map((scope) => scope.target_part_code)).toEqual(["SLEG", "SARM"]);
  });
});
