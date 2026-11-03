import ArchivesPage from "./ArchivesPage";
import { FactoryMonitor } from "../components/FactoryMonitor";
import { PageTitle } from "../components/Workbench";
import { useAuth } from "../auth";
import type { ArchiveDataDomain } from "../archiveApi";
import { FileUp } from "lucide-react";
import "../components/factoryLedger.css";

export default function FactoryPage({ navigate }: {
  navigate: (view: "import" | "archives" | "factory" | "archive" | "curves" | "execution" | "submissions", id?: string, domain?: ArchiveDataDomain) => void;
}) {
  const { session } = useAuth();
  const canUpload = ["SYSTEM_ADMIN", "TEST_EXECUTOR"].includes(session?.user.role ?? "");
  return <div className="factory-console"><PageTitle eyebrow="FACTORY TESTS" title="工厂测试" description="台架状态、待办处理与测试记录" trailing={canUpload ? <button type="button" className="factory-upload-button" onClick={() => navigate("import", undefined, "FACTORY")}><FileUp aria-hidden="true" />上传测试记录</button> : undefined} />
    <FactoryMonitor navigate={navigate} />
    <ArchivesPage domain="FACTORY" navigate={navigate} embedded />
  </div>;
}
