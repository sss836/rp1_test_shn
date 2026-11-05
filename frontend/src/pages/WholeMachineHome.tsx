import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, RefreshCw } from "lucide-react";
import { useAuth } from "../auth";
import { robotFactoryApi, type FactoryRobot } from "../robotFactoryApi";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/Workbench";
import { HomePage } from "./HomePage";
import type { TargetPartCode } from "../types";
import "./wholeMachineHome.css";

function RobotCard({ robot, online, onRecords }: { robot: FactoryRobot; online: boolean; onRecords: (id: string, sourceId: string) => void }) {
  return <article className="whole-robot-card" aria-label={`机器人 ${robot.robot_id}`}>
    <header><img src="/assets/rp1-waving-robot.webp" width="46" height="58" alt="" aria-hidden="true" /><div><small>ROBOT</small><h2>{robot.robot_id}</h2>{robot.source_kind === "simulated" && <span className="whole-robot-simulated">模拟数据</span>}</div><span className={`whole-robot-online ${online ? "is-online" : "is-offline"}`}><i />{online ? "在线" : "离线"}</span></header>
    <dl className="whole-robot-identity"><div><dt>SN</dt><dd>{robot.serial_number || "未关联"}</dd></div><div><dt>工位</dt><dd>{robot.station_name || "未登记"}</dd></div><div><dt>机型</dt><dd>{robot.model || "未提供"}</dd></div></dl>
    <div className="whole-robot-cases"><h3>已做测试用例</h3>{robot.cases.length ? <ul>{robot.cases.map(item => <li key={item.case_code}><code>{item.case_code}</code><span>{item.test_name}</span></li>)}</ul> : <p>暂无已上报的测试用例</p>}</div>
    <button type="button" onClick={() => onRecords(robot.robot_id, robot.source_id)}>查看测试记录<ArrowRight aria-hidden="true" /></button>
  </article>;
}

export default function WholeMachineHome({ navigate, onRecords }: {
  navigate: (view: "assets" | "test-case", id?: string, part?: TargetPartCode) => void;
  onRecords: (robotId: string, sourceId: string) => void;
}) {
  const { session } = useAuth();
  const [view, setView] = useState<"overview" | "focus">("overview");
  const [filter, setFilter] = useState("all"), [search, setSearch] = useState("");
  const [now, setNow] = useState(Date.now);
  const query = useQuery({ queryKey: ["factory-robots", session?.user.id], queryFn: ({ signal }) => robotFactoryApi.robots(signal), enabled: view === "overview", refetchInterval: 5000, retry: false });
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const elapsed = Math.max(0, now - query.dataUpdatedAt);
  const timeout = (query.data?.offline_after_seconds ?? 45) * 1000;
  const live = !query.isError && query.dataUpdatedAt > 0 && elapsed < timeout;
  const online = (robot: FactoryRobot) => {
    const age = Date.parse(query.data?.as_of_at ?? "") + elapsed - Date.parse(robot.last_seen_at ?? "");
    return live && robot.online && Number.isFinite(age) && age >= 0 && age < timeout;
  };
  const robots = query.data?.items ?? [];
  const visible = robots.filter(robot => (filter === "all" || (filter === "online" ? online(robot) : !online(robot))) && [robot.robot_id, robot.serial_number, robot.station_name].some(value => (value ?? "").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())));
  return <div className="whole-machine-home">
    <nav className="whole-home-tabs" aria-label="整机总控视图"><button type="button" aria-pressed={view === "overview"} onClick={() => setView("overview")}>机器人总览</button><button type="button" aria-pressed={view === "focus"} onClick={() => setView("focus")}>单机聚焦</button></nav>
    {view === "focus" ? <><p className="whole-focus-scope">以下沿用平台整机系统范围视图，数据按已关联的测试范围汇总；单台机器人的结果请从“机器人总览”进入测试记录。</p><HomePage assetKind="WHOLE_MACHINE" navigate={navigate} /></> : <section className="whole-robot-overview">
      <PageTitle eyebrow="RELIABILITY TEST CONTROL" title="整机系统测试总控" trailing={<button type="button" className="whole-refresh" disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw aria-hidden="true" />{query.isFetching ? "读取中" : "刷新"}</button>} />
      <div className="whole-overview-summary"><span>机器人总览</span><strong>本页 {robots.length} 台</strong><span>{robots.filter(online).length} 在线</span><span>{robots.filter(robot => !online(robot)).length} 离线</span></div>
      {query.data?.truncated && <p role="status" className="whole-connection-notice">当前仅显示来源接口返回的前 {robots.length} 台，搜索和连接状态筛选只作用于本页；更多历史请在工厂结果中按 SN 或上位机查询。</p>}
      <div className="whole-overview-filters"><div role="group" aria-label="机器人连接状态">{[["all", "全部"], ["online", "在线"], ["offline", "离线"]].map(([value, label]) => <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}</div><label><span>搜索机器人</span><input type="search" value={search} maxLength={200} placeholder="机器人 / SN / 工位" onChange={event => setSearch(event.target.value)} /></label></div>
      {query.isError && query.data && <p role="alert" className="whole-connection-notice">连接状态读取失败，已撤下在线标识；保留已收到的机器人身份与历史用例。请刷新重试。</p>}
      {query.isPending ? <LoadingState label="正在读取机器人总览" /> : !query.data ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : !robots.length ? <EmptyState title="暂无已登记的机器人" description="机器人和已做用例由实际监控来源及整机结果记录提供。" /> : !visible.length ? <p className="whole-connection-notice">没有符合筛选条件的机器人。</p> : <div className="whole-robot-grid">{visible.map(robot => <RobotCard key={`${robot.source_id}:${robot.robot_id}`} robot={robot} online={online(robot)} onRecords={onRecords} />)}</div>}
    </section>}
  </div>;
}
