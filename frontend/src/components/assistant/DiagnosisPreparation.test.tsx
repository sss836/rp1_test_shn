// @vitest-environment jsdom
import {cleanup,render,screen} from "@testing-library/react";
import {afterEach,expect,it} from "vitest";
import {DiagnosisPreparation} from "./DiagnosisPreparation";
import type {AssistantCard} from "../../assistantApi";
afterEach(cleanup);
function card(data:Record<string,unknown>):AssistantCard{return{type:"diagnosis_preparation",title:"查证",source:"当前资料",updated_at:"2026-11-01",data};}
it("makes partial progress, unread files, evidence counts and time uncertainty visible without JSON",()=>{
 render(<DiagnosisPreparation card={card({status:"partial",evidence_complete:false,root_cause_confirmed:false,selected_files:[{path:"/logs/test.csv"}],unselected_file_count:2,evidence:[{type:"local_diagnostic",data:{path:"/logs/test.csv",complete:false,source_sha256:"filehash",data:{rows_analyzed:1250,gap_count:3,time_basis:"relative_origin_unknown"}}}],issues:[{reason:"在本次已检索且可读的实例目录中未找到可核对的日志"}]})}/>);
 expect(screen.getByText("只完成了一部分检查，尚不能据此判断全部数据。")).toBeTruthy();expect(screen.getByText(/已检查 1,250 行CSV记录/)).toBeTruthy();expect(screen.getByText(/还有 2 个找到的文件未纳入这次检查/)).toBeTruthy();expect(screen.getByText(/时间起点或时区还没有核对/)).toBeTruthy();expect(screen.getByText(/发现 3 处时间间隔异常/)).toBeTruthy();expect(screen.getByLabelText("仍缺少的资料").textContent).toContain("未找到可核对的日志");const details=screen.getByText("查看资料、范围与数值明细").closest("details")!;expect(details.open).toBe(false);expect(details.textContent).toContain("filehash");
});
it("describes complete evidence as checked materials rather than confirmed root cause",()=>{
 render(<DiagnosisPreparation card={card({status:"ready",evidence_complete:true,root_cause_confirmed:false,evidence:[{type:"local_diagnostic",data:{path:"gateway.log",complete:true,data:{lines_analyzed:500,match_count:4}}}]})}/>);expect(screen.getByText(/本次选定范围的资料已检查完/)).toBeTruthy();expect(screen.getByText(/已检查 500 行日志/)).toBeTruthy();expect(screen.getByText(/故障原因还没有确认/)).toBeTruthy();expect(screen.queryByText("故障原因已确认")).toBeNull();
});
it("routes waiting to the existing permission workflow and does not invent another approve action",()=>{
 render(<DiagnosisPreparation card={card({status:"permission_required",evidence_complete:false,evidence:[],issues:[]})}/>);expect(screen.getByText(/请在资料申请中决定/)).toBeTruthy();expect(screen.queryByRole("button",{name:/允许/})).toBeNull();
});
it("states unknown time units and mixed clocks in the default summary",()=>{
 render(<DiagnosisPreparation card={card({status:"partial",evidence:[{data:{data:{time_basis:"numeric_unit_unknown"}}},{data:{data:{time_basis:"mixed_time_bases"}}}]})}/>);
 expect(screen.getByText(/时间单位还没有确认/)).toBeTruthy();expect(screen.getByText(/混用了不同的时间口径/)).toBeTruthy();
});
it("does not ask again when permission is already approved and file evidence is still pending",()=>{
 render(<DiagnosisPreparation card={card({status:"waiting_for_data",phase:"awaiting_permission",evidence_complete:false,evidence:[],issues:[]})}/>);
 expect(screen.getByText(/已经允许查看，正在等候原上位机返回资料，不需要重复确认/)).toBeTruthy();expect(screen.queryByText(/还需要你允许/)).toBeNull();
});

it("reads uploaded CSV row ranges and time-analysis gaps from their actual nested fields",()=>{
 render(<DiagnosisPreparation card={card({status:"partial",evidence_complete:false,root_cause_confirmed:false,evidence:[{type:"diagnostic_attachment",data:{full_file_statistics:false,range:{rows_analyzed:200,data_rows_start:200,data_rows_end_exclusive:400},time_analysis:{gap_count:2,time_basis:"relative_origin_unknown"}}}]})}/>);
 expect(screen.getByText(/已检查 200 行CSV记录（数据第201–400行，不含表头）/)).toBeTruthy();expect(screen.getByText(/发现 2 处时间间隔异常/)).toBeTruthy();expect(screen.getByText(/时间起点或时区还没有核对/)).toBeTruthy();
});
