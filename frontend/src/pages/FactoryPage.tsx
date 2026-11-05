import ArchivesPage from "./ArchivesPage";
import { FactoryMonitor } from "../components/FactoryMonitor";
import { PageTitle } from "../components/Workbench";
import { useAuth } from "../auth";
import type { ArchiveDataDomain } from "../archiveApi";
import { FileUp } from "lucide-react";
import { RobotFactoryResults } from "../components/RobotFactoryResults";
import "../components/factoryLedger.css";

export default function FactoryPage({ navigate, robotId, sourceId, initialArchiveId }: {
  navigate: (view: "import" | "archives" | "factory" | "archive" | "curves" | "execution" | "submissions", id?: string, domain?: ArchiveDataDomain) => void;
  robotId?: string;
  sourceId?: string;
  initialArchiveId?: string;
}) {
  const { session } = useAuth();
  const canUpload = ["SYSTEM_ADMIN", "TEST_EXECUTOR"].includes(session?.user.role ?? "");
  const canMonitor = ["SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER"].includes(session?.user.role ?? "");
  const filtered = Boolean(robotId || sourceId || initialArchiveId);
  return <div className="factory-console"><PageTitle eyebrow="FACTORY TESTS" title="工厂测试" description={filtered ? "当前机器人与上位机来源的整机结果汇总" : "台架状态、待办处理与测试记录"} trailing={filtered ? <button type="button" className="factory-upload-button" onClick={() => navigate("factory", undefined, "FACTORY")}>查看全部工厂测试</button> : canUpload ? <button type="button" className="factory-upload-button" onClick={() => navigate("import", undefined, "FACTORY")}><FileUp aria-hidden="true" />上传测试记录</button> : undefined} />
    <RobotFactoryResults key={`${sourceId ?? ""}:${robotId ?? ""}:${initialArchiveId ?? ""}`} robotId={robotId} sourceId={sourceId} initialArchiveId={initialArchiveId} />
    {!filtered && <>{canMonitor && <FactoryMonitor navigate={navigate} />}<ArchivesPage domain="FACTORY" navigate={navigate} embedded /></>}
  </div>;
}
