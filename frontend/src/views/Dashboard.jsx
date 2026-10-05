import { useEffect, useMemo, useState } from "react";
import { api, resolveUrl } from "../api";
import { Icon } from "../icons";
import { Button, EmptyState, ProgressSteps, StatusDot } from "../components/Ui";

const statusText = {
  created: "已登记", queued: "等待处理", running: "处理中", processing: "处理中", paused: "已暂停",
  completed: "已完成", completed_with_warnings: "完成但有告警", blocked: "被阻断", failed: "失败", cancelled: "已取消",
};

function jobForProject(project, jobs) {
  return jobs.find((item) => String(item.project_id) === String(project?.id)) || project?.current_job || project?.job || null;
}

function anomalyCount(project, anomalies, jobs) {
  const job = jobForProject(project, jobs);
  return Number(project?.exception_count ?? project?.anomaly_count ?? job?.exception_count ?? anomalies.length ?? 0);
}

export function Dashboard({ data, onNewSeries, onImport, onNavigate }) {
  const project = data.selectedProject;
  const job = jobForProject(project, data.jobs);
  const issues = anomalyCount(project, data.anomalies, data.jobs);
  const services = data.health.services || {};
  const [exporting, setExporting] = useState(false);
  const [downloadUrl, setDownloadUrl] = useState("");
  useEffect(() => setDownloadUrl(""), [project?.id]);

  const continueProject = () => data.act(() => api.enqueue(project.id), "处理任务已加入本地队列；进度会以真实阶段更新。");
  const cancelCurrent = () => job?.id && data.act(() => api.cancelJob(job.id), "已请求取消任务；已生成的缓存会保留。");
  const exportProject = async () => {
    if (!project) return;
    setExporting(true);
    const result = await data.act(() => api.exportProject(project.id), "候选成片已生成，可下载保存。");
    setExporting(false);
    const candidate = result?.output_url || result?.download_url || result?.media_url || result?.url;
    if (candidate) setDownloadUrl(resolveUrl(candidate));
  };
  const previewSource = data.preview?.output_url || data.preview?.source_url;
  const previewReady = Boolean(data.preview?.ready && (data.preview?.output_url || project));
  const hasActiveJob = ["queued", "running", "processing", "paused"].includes(String(job?.status || "").toLowerCase());

  return <div className="dashboard-view">
    <header className="page-heading">
      <div><h1>继续处理</h1><p>从上次停止的位置继续，完成当前项目的配音制作。</p></div>
      <div className="heading-actions"><Button variant="secondary" icon="upload" onClick={onImport} disabled={!data.health.connected}>导入视频</Button><Button icon="plus" onClick={onNewSeries} disabled={!data.health.connected}>新建系列</Button></div>
    </header>

    <div className="workspace-grid">
      <main>
        {project ? <section className="active-project panel">
          <div className="project-title-row">
            <div className="project-thumb"><Icon name="play" size={28}/></div>
            <div className="project-copy"><span>当前项目</span><h2>{project.series_name || project.series?.name || "未命名系列"}</h2><h3>{project.title || project.name || `项目 ${project.id}`}</h3><p>{job?.message || project.status_message || `当前状态：${statusText[project.status] || project.status || "等待真实处理"}`}</p></div>
            <div className="project-actions"><Button icon="play" onClick={continueProject} disabled={!data.health.connected || hasActiveJob}>继续任务</Button>{hasActiveJob && <Button variant="secondary" icon="warning" onClick={cancelCurrent}>取消任务</Button>}<Button variant="secondary" icon="warning" onClick={() => onNavigate("exceptions")}>查看异常</Button></div>
          </div>
          <ProgressSteps project={project} jobs={data.jobs}/>
          <button className={`issue-strip ${issues ? "has-issues" : ""}`} onClick={() => onNavigate("exceptions")}>
            <Icon name="warning" size={21}/><span><strong>{issues ? `需要处理 ${issues} 项` : "暂无已报告异常"}</strong><small>{issues ? "自动流程遇到需要确认的问题。" : "不可测质量不会被伪装为已验证。"}</small></span><Icon name="chevron"/>
          </button>
          <SegmentPeek segments={data.segments}/>
        </section> : <EmptyState title={data.health.connected ? "还没有项目" : "等待连接后端"} text={data.health.connected ? "先新建系列，再登记一个视频的本地绝对路径。" : "页面不会使用虚拟任务冒充真实处理结果。"}/>} 

        <section className="recent-section">
          <div className="section-title"><h2>最近的系列 / 项目</h2><button onClick={() => onNavigate("series")}>查看全部 <Icon name="chevron" size={16}/></button></div>
          {data.projects.length ? <div className="project-table" role="table">
            <div className="project-row header" role="row"><span>系列名称</span><span>项目</span><span>状态</span><span>最近更新</span></div>
            {data.projects.slice(0, 4).map((item) => <button className="project-row" role="row" key={item.id} onClick={() => { data.setSelectedProjectId(item.id); onNavigate("series"); }}>
              <span>{item.series_name || item.series?.name || "未命名系列"}</span><strong>{item.title || item.name || `项目 ${item.id}`}</strong><span><StatusDot value={item.status}/>{statusText[item.status] || item.status || "未知"}</span><span>{item.updated_at ? new Date(item.updated_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"}</span>
            </button>)}
          </div> : <p className="quiet-empty">连接后显示真实项目记录。</p>}
        </section>
      </main>

      <aside className="inspection-column">
        <section className="preview-panel panel">
          <h2>预览与检查</h2>
          <div className="video-frame">
            {project && data.health.connected && previewSource ? <video controls preload="metadata" src={resolveUrl(previewSource)}/> : <div><Icon name="play" size={32}/><span>{project ? "检查完成后可预览" : "完成混音后可预览"}</span></div>}
          </div>
          <div className="preview-label">{previewReady ? "候选成片预览" : data.preview?.source_url ? "源片预览（尚无候选成片）" : "尚无可用媒体"}</div>
          <div className="preview-actions"><Button variant="secondary" icon="play" disabled={!project || !previewSource}>试听</Button><Button icon="download" busy={exporting} onClick={exportProject} disabled={!project || !data.health.connected || !previewReady}>{previewReady ? "生成并下载 MP4" : "完成处理后导出"}</Button></div>
          {downloadUrl && <a className="download-result" href={downloadUrl} download>下载刚生成的候选 MP4</a>}
          {data.preview?.subtitle_url && <a className="subtitle-result" href={resolveUrl(data.preview.subtitle_url)} download>下载目标语言字幕</a>}
          <dl className="preview-meta"><div><dt>项目状态</dt><dd>{project ? statusText[project.status] || project.status || "未知" : "未选择"}</dd></div><div><dt>目标语言</dt><dd>{project?.target_language || "—"}</dd></div><div><dt>分级</dt><dd>{project?.level || project?.target_level || "—"}</dd></div><div><dt>语速</dt><dd>{project?.speed != null ? `${project.speed}×` : "—"}</dd></div></dl>
          {data.preview?.warnings?.length > 0 && <div className="preview-warning">{data.preview.warnings.join("；")}</div>}
        </section>
        <section className="services-panel panel">
          <div className="section-title"><h2>服务与设置</h2><button onClick={() => onNavigate("settings")}>查看设置 <Icon name="chevron" size={16}/></button></div>
          {[['faster_whisper', '语音转写服务'], ['omnivoice', '语音合成服务'], ['text_rewriter', '翻译与改写服务'], ['bandit', '对白分离模型'], ['diarization', '角色聚类']].map(([key, label]) => {
            const service = services[key] || services[{ faster_whisper: "transcription", omnivoice: "dubbing", text_rewriter: "rewrite" }[key]];
            const value = service?.status || service || (data.health.connected ? "unknown" : "offline");
            const serviceText = { ok: "服务正常", healthy: "服务正常", ready: "服务正常", configured: "已配置", blocked: "被阻断", missing: "缺少依赖", offline: "未连接", error: "检查失败" }[value] || "尚未核实";
            return <div className="service-row" key={key}><span>{label}</span><em><StatusDot value={value}/>{service?.label || serviceText}</em></div>;
          })}
        </section>
      </aside>
    </div>
  </div>;
}

function SegmentPeek({ segments }) {
  const dialogue = segments.filter((segment) => segment.kind === "dialogue");
  const item = dialogue.find((segment) => segment.exception || segment.duration_delta > 0) || dialogue[0];
  if (!item) return <div className="segment-peek"><div className="section-title"><h2>当前片段示例</h2></div><p className="quiet-empty">尚无句级结果。</p></div>;
  return <div className="segment-peek"><div className="section-title"><h2>当前片段示例</h2></div><div className="segment-peek-grid"><span><small>原文</small>{item.source_text || item.original_text || "—"}</span><span><small>目标文本</small>{item.target_text || "—"}</span><span><small>角色</small>{item.speaker_name || item.character_name || item.speaker_key || "待匹配"}</span><span className={Number(item.duration_delta) > 0 ? "over" : ""}><small>时长差</small>{item.duration_delta == null ? "—" : `${Number(item.duration_delta) > 0 ? "+" : ""}${Number(item.duration_delta).toFixed(2)} 秒`}</span></div></div>;
}
