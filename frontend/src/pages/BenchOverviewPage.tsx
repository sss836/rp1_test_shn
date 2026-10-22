import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useAuth } from "../auth";
import { agePresence, associateStations, benchCode, disableReason, getBenchPresence, getRegisteredStations } from "../benchOverview";
import { EmptyState, ErrorState, LoadingState, PageTitle, Panel } from "../components/Workbench";
import { isStationTesting, stationLabels, type HmiStation } from "../hmiPresence";
import { formatDate, type View } from "../navigation";
import { HmiStationCard } from "./HmiStationsPage";
import { StationCreateForm } from "./SetupPages";
import "./benchOverview.css";

const typeLabels = { WHOLE_MACHINE: "整机", MODULE: "模块", SHARED: "共用" };

function presenceLabel(items: HmiStation[], live: boolean, admin: boolean) {
  if (!live) return "状态待确认";
  if (!items.length) return admin ? "无上报记录" : "无可见上报";
  const online = items.filter((item) => item.online);
  if (!online.length) return "离线";
  if (online.length > 1) return `${online.length} 台上位机在线`;
  return stationLabels[online[0].state];
}

export default function BenchOverviewPage({ navigate,initialStationId }: { navigate: (view: View) => void;initialStationId?:string }) {
  const { session } = useAuth();
  const admin = session?.user.role === "SYSTEM_ADMIN";
  const canCreate = ["SYSTEM_ADMIN", "TEST_EXECUTOR"].includes(session?.user.role ?? "");
  const [automatic, setAutomatic] = useState(true);
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  const [focused,setFocused]=useState(initialStationId??"");
  const [type, setType] = useState("all");
  const [filter, setFilter] = useState("all");
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const registry = useQuery({
    queryKey: ["bench-registry", session?.user.id], queryFn: ({ signal }) => getRegisteredStations(signal),
    refetchInterval: automatic ? 60_000 : false, retry: 1
  });
  const presence = useQuery({
    queryKey: ["bench-presence", session?.user.id], queryFn: ({ signal }) => getBenchPresence(signal),
    refetchInterval: automatic ? 5000 : false, retry: 1
  });
  const live = automatic && !presence.isError && !!presence.data && now - presence.dataUpdatedAt < 15_000;
  const hmis = useMemo(() => (presence.data?.items ?? []).map((item) => agePresence(item, presence.data!.as_of_at, now - presence.dataUpdatedAt)), [presence.data, presence.dataUpdatedAt, now]);
  const stations = registry.data ?? [];
  const { byCode, unmatched } = associateStations(stations, hmis);
  const needle = search.trim().toLocaleLowerCase();
  const rows = stations.filter((station) => {
    if(focused&&station.id!==focused)return false;
    const items = byCode.get(benchCode(station.code)) ?? [];
    const matchesSearch = [station.code, station.name, station.site_code, station.site_name, station.lab_code, station.lab_name, station.site_location].join(" ").toLocaleLowerCase().includes(needle);
    const matchesState = filter === "all" || (filter === "available" ? station.available : filter === "disabled" ? !station.available : filter === "unreported" ? live && !items.length : filter === "online" ? live && items.some((item) => item.online) : filter === "running" ? live && items.some(isStationTesting) : live && !!items.length && items.every((item) => !item.online));
    return matchesSearch && matchesState && (type === "all" || station.station_type === type);
  }).sort((a, b) => a.code.localeCompare(b.code, "zh-CN", { numeric: true }));
  const onlineCount = stations.filter((station) => byCode.get(benchCode(station.code))?.some((item) => item.online)).length;
  const testingCount = stations.filter((station) => byCode.get(benchCode(station.code))?.some(isStationTesting)).length;
  const fetching = registry.isFetching || presence.isFetching;
  const refresh = () => { void registry.refetch(); void presence.refetch(); };
  return (
    <div className="workbench bench-overview-page">
      <PageTitle eyebrow="BENCH OVERVIEW" title="台架总览" description="查看全部已建档台架的编号、名称、位置和运行情况。整机、模块和共用台架在此统一显示。"
        trailing={<div className="bench-actions">{canCreate && <button type="button" className="primary-button" aria-expanded={creating} onClick={() => setCreating((value) => !value)}>{creating ? "收起新建" : "新建台架"}</button>}<button type="button" className="subtle-button" onClick={() => navigate("stations")}>上位机监控</button><button type="button" className="subtle-button" onClick={refresh} disabled={fetching}><RefreshCw aria-hidden="true" />{fetching ? "刷新中…" : "立即刷新"}</button></div>} />
      {canCreate && creating && <StationCreateForm after={() => { setCreating(false); setSearch(""); setType("all"); setFilter("all"); }} />}
      <p className="hmi-scope-note">台架名称来自平台建档。运行状态按上位机填写的台架编号关联，不能代替现场设备身份核对；此页不计入正式测试统计。</p>
      {!admin && <p className="hmi-snapshot-note">台架清单包含全部已建档台架；运行状态仅包含当前账号可见的上位机。“无可见上报”不代表台架离线或无人使用。</p>}
      {registry.isPending ? <LoadingState label="正在读取台架清单" /> : registry.isError && !registry.data ? <ErrorState error={registry.error} retry={refresh} /> : <>
        {registry.isError && <p role="alert">台架清单刷新失败，以下为上次读取的建档信息。<button type="button" onClick={refresh}>重试</button></p>}
        <section className="bench-summary" aria-label="台架概况">
          <div><span>已建档台架</span><strong>{stations.length}</strong></div>
          <div><span>已启用</span><strong>{stations.filter((station) => station.available).length}</strong></div>
          <div><span>{admin ? "上位机在线的台架" : "可见上位机在线的台架"}</span><strong>{live ? onlineCount : "—"}</strong></div>
          <div><span>{admin ? "测试运行中的台架" : "可见测试运行中的台架"}</span><strong>{live ? testingCount : "—"}</strong></div>
        </section>
        <section className="bench-filters" aria-label="台架筛选">
          {focused&&<p>当前定位台架：{focused}<button type="button" onClick={()=>setFocused("")}>返回全部台架</button></p>}
          <label>搜索台架<input type="search" placeholder="编号、名称、站点或实验室" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
          <label>台架类型<select value={type} onChange={(event) => setType(event.target.value)}><option value="all">全部类型</option>{Object.entries(typeLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label>台架状态<select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">全部状态</option><option value="available">已启用</option><option value="disabled">已停用（含所属位置）</option><option value="online">上位机在线</option><option value="running">测试运行中</option><option value="offline">上位机离线</option><option value="unreported">{admin ? "无上报记录" : "无可见上报"}</option></select></label>
          <label className="hmi-auto-refresh"><input type="checkbox" checked={automatic} onChange={(event) => setAutomatic(event.target.checked)} />每 5 秒刷新运行状态</label>
        </section>
        <p className="bench-read-time">显示 {rows.length} / {stations.length} 个台架 · 清单读取：{registry.dataUpdatedAt ? formatDate(new Date(registry.dataUpdatedAt).toISOString()) : "暂无"} · 状态采样截止：{formatDate(presence.data?.as_of_at)}</p>
        {presence.isPending && <p role="status">正在读取上位机状态，台架清单仍可查看。</p>}
        {presence.isError && <p className="hmi-snapshot-note" role="alert">上位机状态读取失败，当前运行情况待确认。<button type="button" onClick={() => void presence.refetch()}>重试读取状态</button></p>}
        {!automatic && <p className="hmi-snapshot-note" role="status">自动刷新已暂停，运行信息仅供查看历史快照。</p>}
        {automatic && presence.data && !presence.isError && !live && <p className="hmi-snapshot-note" role="status">页面未及时取得最新状态，当前运行情况待确认。</p>}
        {rows.length ? <div className="table-scroll"><table className="data-table bench-table" aria-label="已建档台架清单"><thead><tr><th>台架编号 / 名称</th><th>站点 / 实验室</th><th>类型</th><th>启用状态</th><th>运行状态</th><th>测试信息</th><th>最近心跳 / 详情</th></tr></thead><tbody>{rows.map((station) => {
          const items = byCode.get(benchCode(station.code)) ?? [];
          const latest = items.map((item) => item.last_seen_at).sort().at(-1);
          const active = items.filter(isStationTesting);
          const runSources = live ? active : [];
          return <tr key={station.id}>
            <td><strong>{station.code}</strong><small>{station.name}</small></td>
            <td>{station.site_name} · {station.site_code}<small>{station.lab_name} · {station.lab_code}</small>{station.site_location && <small>{station.site_location}</small>}</td>
            <td>{typeLabels[station.station_type]}</td>
            <td><span className={`bench-availability ${station.available ? "enabled" : "disabled"}`}>{disableReason(station)}</span></td>
            <td>{presenceLabel(items, live, admin)}{live && items.some((item) => item.plc.driver === "mock") && <small>含模拟 PLC</small>}</td>
            <td>{runSources.length ? runSources.flatMap((item) => item.runs.filter((run) => run.state === "running" || run.state === "stopping").map((run) => <div className="bench-current-run" key={`${item.installation_id}-${run.session_key}`}><strong>{run.execution_code || run.test_id || "未编号测试"}</strong><small>{run.sample_id || "样品未填写"} · {run.case_code || "用例未填写"}</small><small>{item.operator_display_name}（{item.operator_name}）</small></div>)) : items.length ? "查看上位机详情" : "—"}</td>
            <td>{formatDate(latest)}{items.length > 0 && <details><summary>{items.length} 台上位机详情</summary><div className="bench-hmi-details">{items.map((item) => <HmiStationCard key={item.installation_id} station={item} live={live} />)}</div></details>}</td>
          </tr>;
        })}</tbody></table></div> : <EmptyState title={stations.length ? "没有符合条件的台架" : "尚无台架建档"} description={stations.length ? "调整搜索或筛选条件后重试。" : "管理员可在“测试计划 → 台架建档”中登记现场台架。"} />}
        {unmatched.length > 0 && <Panel title={`未关联到唯一建档台架的上位机（${unmatched.length} 台）`} description="台架编号未填写、不在清单中或匹配不唯一。核对现场台架后，在 HMI 顶栏填写对应编号；名称不会用于自动匹配。"><div className="hmi-station-grid">{unmatched.map((item) => <HmiStationCard key={item.installation_id} station={item} live={live} />)}</div></Panel>}
      </>}
      <p className="hmi-monitor-footnote">台架清单每 60 秒刷新。上位机每 10 秒上报，超过 45 秒无心跳显示离线；采样超过 20 秒显示状态待确认。离线不代表设备已停止，电柜供电也不代表测试已开始。</p>
    </div>
  );
}
