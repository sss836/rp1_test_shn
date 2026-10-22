import { readFileSync } from "node:fs";

function secretFile(path) {
  if (!path) throw new Error("server secret file is required");
  const value = readFileSync(path, "utf8").trim();
  if (value.length < 32) throw new Error("server secret is too short");
  return value;
}

export function loadConfig(env = process.env) {
  if (env.DATABASE_URL || env.PGHOST || env.CAN_CHANNEL || env.PLC_HOST) {
    throw new Error("Pi must not receive database or device configuration");
  }
  const mode = env.PI_MODEL_MODE ?? "live";
  if (!["live", "mock"].includes(mode)) throw new Error("unsupported model mode");
  if (mode === "mock" && env.APP_ENV !== "test") throw new Error("mock requires test environment");
  return Object.freeze({
    mode,
    // There is no configurable arbitrary model URL or model-supplied URL tool.
    modelUrl: mode === "live" ? "https://api.deepseek.com" : "http://model-mock:3100/v1",
    apiKey: mode === "live" ? secretFile(env.DEEPSEEK_API_KEY_FILE) : "synthetic-model-key-for-isolated-test",
    bridgeSecret: secretFile(env.RP1_BRIDGE_SECRET_FILE),
    toolUrl: "http://api:8000/api/v1/assistant/tools/list_hmi_stations",
  });
}
