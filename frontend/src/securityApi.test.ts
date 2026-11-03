import { describe, expect, it } from "vitest";
import { parseSession } from "./securityApi";

const session = {
  user: {
    id: "user-id",
    username: "operator",
    display_name: "Operator",
    role: "TEST_EXECUTOR",
    kind: "HUMAN",
    enabled: true,
    must_change_password: false
  },
  campaign_access: [],
  session_type: "BROWSER",
  expires_at: "2026-09-22T12:00:00Z"
};

describe("session response validation", () => {
  it("accepts a backend-compatible session", () => {
    expect(parseSession(session)).toEqual(session);
  });

  it("accepts the fixed factory reviewer role without granting a research campaign", () => {
    const scoped = { ...session, user: { ...session.user, role: "FACTORY_REVIEWER" } };
    expect(parseSession(scoped).user.role).toBe("FACTORY_REVIEWER");
    expect(parseSession(scoped).campaign_access).toEqual([]);
  });

  it("rejects an unexpected role before it reaches the UI", () => {
    expect(() => parseSession({
      ...session,
      user: { ...session.user, role: "SUPER_ADMIN" }
    })).toThrow(/无效的会话数据/);
  });
});
