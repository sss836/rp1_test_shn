import ArchivesPage from "./ArchivesPage";
import { FactoryMonitor } from "../components/FactoryMonitor";
import { PageTitle } from "../components/Workbench";
import { useAuth } from "../auth";
import type { ArchiveDataDomain } from "../archiveApi";

export default function FactoryPage({ navigate }: {
  navigate: (view: "import" | "archives" | "factory" | "archive" | "curves" | "execution" | "submissions", id?: string, domain?: ArchiveDataDomain) => void;
}) {
  const { session } = useAuth();
  const canUpload = ["SYSTEM_ADMIN", "TEST_EXECUTOR"].includes(session?.user.role ?? "");
  return <div className="factory-console"><PageTitle eyebrow="FACTORY TESTS" title="工厂测试" description="查看在线台架、当前测试、完成待处理及异常；下方保留工厂记录台账。" trailing={canUpload ? <button type="button" onClick={() => navigate("import", undefined, "FACTORY")}>上传测试记录</button> : undefined} />
    <FactoryMonitor navigate={navigate} />
    <h2 className="factory-record-title">工厂测试记录</h2>
    <ArchivesPage domain="FACTORY" navigate={navigate} embedded />
  </div>;
}
