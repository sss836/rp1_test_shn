import type { TargetPartCode } from "./types";

const registrationPartNames: Record<TargetPartCode, string> = {
  SYS: "整机", ARM: "7自由度手臂", SARM: "5自由度手臂", LEG: "6自由度腿部", SLEG: "5自由度腿部",
  UPPER: "上肢", LOWER: "下肢", WAISTHIP: "腰胯", CHEST: "胸腔", HEAD: "头部", BAT: "动力电池"
};

export function parseAssetNumber(code: string) {
  const match = /^(RP[0-9]+\.[0-9]+)-(SYS|ARM|SARM|LEG|SLEG|UPPER|LOWER|WAISTHIP|CHEST|HEAD|BAT)-([0-9]{3})$/.exec(code.trim());
  if (!match) return null;
  const part = match[2] as TargetPartCode;
  return { family: match[1], part, partName: registrationPartNames[part], sequence: match[3], prefix: `${match[1]}-${part}` };
}

export function creationScenarioName(scenario: "PRODUCTION" | "RND_TEST") {
  return scenario === "PRODUCTION" ? "生产" : "研发测试";
}
