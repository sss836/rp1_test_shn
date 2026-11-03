import {TelemetryChart,type CurveSeries} from "../components/TelemetryChart";
export default function ShowcaseCurves({series}:{series:CurveSeries[]}){return <section><h2>精选历史曲线</h2><p>仅反映本主题获准历史区间；精选内容不代替全部测试的总体判定。</p>{[...new Set(series.map(s=>s.unit))].map(unit=><TelemetryChart key={unit} elapsed series={series.filter(s=>s.unit===unit)}/>)}</section>;}
