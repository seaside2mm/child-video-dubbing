import { useEffect, useMemo, useState } from "react";
import { api, resolveUrl } from "../api";
import { Button, EmptyState, StatusDot } from "../components/Ui";

export function Projects({ data, onImport }) {
  const [drafts, setDrafts] = useState({});
  const project = data.selectedProject;
  const currentJob = data.jobs.find((job) => String(job.project_id) === String(project?.id)) || project?.current_job || project?.job;
  const pendingStage = project?.pending_confirmation_stage || project?.checkpoint?.pending_confirmation_stage;
  useEffect(() => setDrafts({}), [project?.id]);
  const rows = useMemo(() => data.segments.map((segment) => ({ ...segment, ...drafts[segment.id] })), [data.segments, drafts]);

  const save = async (row) => {
    const patch = drafts[row.id];
    if (!patch || !project) return;
    const result = await data.act(() => api.updateSegment(project.id, row.id, patch), "句级修改已保存；相关配音、字幕、混音与成片缓存需重新生成。");
    if (result) setDrafts((value) => { const next = { ...value }; delete next[row.id]; return next; });
  };
  const start = () => project && data.act(() => api.enqueue(project.id), "任务已加入队列；请在自动处理页查看真实进度。");
  const cancel = () => currentJob?.id && data.act(() => api.cancelJob(currentJob.id), "已请求取消任务；已生成的缓存会保留。");
  const active = ["queued", "running", "processing", "paused"].includes(String(currentJob?.status || "").toLowerCase());

  return <div className="standard-view">
    <header className="page-heading"><div><h1>项目</h1><p>查看真实句级时间轴；修改只影响有关台词及其后续产物。</p></div><Button icon="upload" onClick={onImport} disabled={!data.health.connected}>导入视频</Button></header>
    <div className="project-layout">
      <aside className="project-list panel" aria-label="项目列表">
        {data.projects.map((item) => <button key={item.id} className={String(item.id) === String(data.selectedProjectId) ? "selected" : ""} onClick={() => data.setSelectedProjectId(item.id)}><span><StatusDot value={item.status}/>{item.series_name || item.series?.name || "未命名系列"}</span><strong>{item.title || item.name || `项目 ${item.id}`}</strong><small>{statusText(item.status)}</small></button>)}
        {!data.projects.length && <p className="quiet-empty">暂无项目</p>}
      </aside>
      <main className="segment-editor panel">
        {project ? <>
          <div className="section-title segment-heading"><div><h2>{project.title || project.name || `项目 ${project.id}`}</h2><p>{pendingStage ? `当前待确认：${stageName(pendingStage)}。确认后才会开始下一阶段。` : project.source_path || project.source_filename || "源文件路径待读取"}</p></div><div className="segment-job-actions"><Button variant="secondary" onClick={start} disabled={active || Boolean(pendingStage) || !data.health.connected}>{active ? "处理中…" : pendingStage ? `待确认：${stageName(pendingStage)}` : "继续任务"}</Button>{active && <Button variant="ghost" onClick={cancel}>取消任务</Button>}</div></div>
          <div className="segment-table-header"><span>时间 / 角色</span><span>原文</span><span>目标文本</span><span>语速</span><span>试听 / 操作</span></div>
          <div className="segment-list">
            {rows.map((row) => <SegmentRow key={row.id} row={row} data={data} project={project} pendingStage={pendingStage} drafts={drafts} setDrafts={setDrafts} save={save}/>) }
          </div>
          {!rows.length && <EmptyState title="尚无句级结果" text="先运行真实的检查、分离、转写与角色阶段。"/>}
        </> : <EmptyState title="请选择项目" text="登记视频后会在这里显示句级时间轴。"/>}
      </main>
    </div>
  </div>;
}

function SegmentRow({ row, data, project, pendingStage, drafts, setDrafts, save }) {
  const options = [...data.characters];
  if (row.speaker_key && !options.some((item) => item.speaker_key === row.speaker_key)) options.push({ speaker_key: row.speaker_key, name: row.speaker_name || row.speaker_key });
  const speakerKey = row.speaker_key || "";
  const audioUrl = row.audio_url || (row.status === "synthesized" ? api.mediaUrl(project.id, `segment/${row.id}`) : "");
  const canEditSpeaker = pendingStage === "characters";
  const canEditText = pendingStage === "rewrite";
  const canEditSpeed = pendingStage === "synthesize";
  const canRegenerate = pendingStage === "synthesize" || (project.status === "completed" && (project.confirmed_stages || project.checkpoint?.confirmed_stages || []).length === 10);
  const update = (patch) => setDrafts((value) => ({ ...value, [row.id]: { ...value[row.id], ...patch } }));
  return <article className={`segment-row ${row.status === "failed" || row.status === "blocked" ? "exception" : ""}`}>
    <div><strong>{formatTime(row.start)}–{formatTime(row.end)}</strong>{options.length ? <select aria-label={`片段 ${row.id} 角色`} disabled={!canEditSpeaker} value={speakerKey} onChange={(event) => update({ speaker_key: event.target.value })}><option value="">待匹配角色</option>{options.map((item) => <option key={item.speaker_key || item.id} value={item.speaker_key}>{item.name || item.speaker_key}</option>)}</select> : <input aria-label={`片段 ${row.id} 角色键`} disabled={!canEditSpeaker} value={speakerKey} placeholder="角色键" onChange={(event) => update({ speaker_key: event.target.value })}/>}</div>
    <p>{row.source_text || row.original_text || "—"}</p>
    <textarea aria-label={`片段 ${row.id} 目标文本`} disabled={!canEditText} value={row.target_text || ""} onChange={(event) => update({ target_text: event.target.value })}/>
    <label><span className="sr-only">语速</span><input type="number" min="0.55" max="1.15" step="0.01" disabled={!canEditSpeed} value={row.speed ?? project.speed ?? 0.82} onChange={(event) => update({ speed: Number(event.target.value) })}/><small>{row.duration_delta == null ? "未测时长" : `时长差 ${Number(row.duration_delta).toFixed(2)} 秒`}</small></label>
    <div className="segment-actions">{audioUrl ? <audio controls preload="none" src={resolveUrl(audioUrl)} aria-label={`片段 ${row.id} 试听`}/> : <span className="audio-unavailable">尚无真实音频</span>}<Button variant="secondary" onClick={() => save(row)} disabled={!drafts[row.id] || !((canEditText && Object.hasOwn(drafts[row.id], "target_text")) || (canEditSpeed && Object.hasOwn(drafts[row.id], "speed")) || (canEditSpeaker && Object.hasOwn(drafts[row.id], "speaker_key")))} title={pendingStage ? "本阶段修改保存后会使受影响的下游产物失效" : "仅在对应阶段待确认时可编辑"}>保存</Button><button className="text-button" disabled={!canRegenerate} onClick={() => data.act(() => api.regenerateSegment(project.id, row.id), "该句已重新生成；完成配音关确认后，才会继续混音等步骤。")}>重生成</button></div>
  </article>;
}

function statusText(value) {
  return ({ created: "已登记", queued: "等待处理", running: "处理中", processing: "处理中", awaiting_confirmation: "待你确认", paused: "已暂停", completed: "已完成", completed_with_warnings: "完成但有告警", blocked: "被阻断", failed: "失败", cancelled: "已取消" })[value] || value || "未知状态";
}

function stageName(value) {
  return ({ probe: "检查媒体", separate: "分离对白", transcribe: "句级转写", diarize: "角色聚类", characters: "准备音色", rewrite: "按等级改写", synthesize: "逐句配音", mix: "混音", subtitle: "生成字幕", export: "输出 MP4" })[value] || value;
}

function formatTime(value) {
  const seconds = Number(value || 0);
  const mins = Math.floor(seconds / 60);
  return `${String(mins).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}
