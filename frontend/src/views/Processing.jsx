import { api } from "../api";
import { Button, EmptyState, StatusDot } from "../components/Ui";

const stageText = { probe: "检查媒体", separate: "分离对白", transcribe: "句级转写", diarize: "角色聚类", characters: "准备音色", rewrite: "按等级改写", synthesize: "逐句配音", mix: "混音", subtitle: "生成字幕", export: "输出 MP4", done: "完成" };
const statusText = { queued: "等待中", running: "处理中", awaiting_confirmation: "待你确认", paused: "已暂停", completed: "已完成", completed_with_warnings: "完成但有告警", blocked: "被阻断", failed: "失败", cancelled: "已取消" };

export function Processing({ data }) {
  const retryable = (value) => ["failed", "blocked", "paused", "cancelled"].includes(value);
  const active = (value) => ["queued", "running", "paused"].includes(value);
  return <div className="standard-view"><header className="page-heading"><div><h1>自动处理</h1><p>单 GPU 队列串行执行；服务中断不会被标记为成功，任务会保留可恢复状态。</p></div><Button variant="secondary" icon="refresh" onClick={() => data.refresh()}>刷新状态</Button></header>
    <section className="queue panel"><div className="queue-head"><span>任务</span><span>项目</span><span>阶段 / 进度</span><span>状态</span><span>操作</span></div>
      {data.jobs.map((job) => <div className="queue-row" key={job.id}><strong>#{job.id}</strong><span>{job.project_title || job.project_id}</span><span className="job-progress"><b>{stageText[job.stage] || job.stage || "等待"}</b><small>{Math.round(Math.max(0, Math.min(1, Number(job.progress || 0))) * 100)}% · {job.message || ""}</small><i><em style={{ width: `${Math.round(Math.max(0, Math.min(1, Number(job.progress || 0))) * 100)}%` }}/></i></span><span><StatusDot value={job.status}/>{statusText[job.status] || job.status || "未知"}</span><span className="job-actions">{active(job.status) && <Button variant="secondary" onClick={() => data.act(() => api.cancelJob(job.id), "已请求取消任务；缓存会保留。")}>取消</Button>}{retryable(job.status) && <Button variant="secondary" onClick={() => data.act(() => api.retryJob(job.id, job.stage), "任务已从最近阶段重新入队。")}>重试</Button>}</span></div>)}
      {!data.jobs.length && <EmptyState title="队列为空" text={data.health.connected ? "登记项目并点击“继续任务”后会显示真实进度。" : "后端未连接，无法创建任务。"}/>}</section>
  </div>;
}
