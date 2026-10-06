import { useEffect, useMemo, useState } from "react";
import { api, resolveUrl } from "../api";
import { Icon } from "../icons";
import { Button, EmptyState, ProgressSteps, StatusDot } from "../components/Ui";

const statusText = {
  created: "已登记", queued: "等待处理", running: "处理中", processing: "处理中", awaiting_confirmation: "待你确认", paused: "已暂停",
  completed: "已完成", completed_with_warnings: "完成但有告警", blocked: "被阻断", failed: "失败", cancelled: "已取消",
};

const stageNames = { probe: "检查媒体", separate: "分离对白", transcribe: "句级转写", diarize: "角色聚类", characters: "准备音色", rewrite: "按等级改写", synthesize: "逐句配音", mix: "混音", subtitle: "生成字幕", export: "输出 MP4", done: "完成" };
const orderedStages = ["probe", "separate", "transcribe", "diarize", "characters", "rewrite", "synthesize", "mix", "subtitle", "export"];

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
  const jumpToReview = () => document.getElementById("stage-review")?.scrollIntoView({ behavior: "smooth", block: "center" });
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
  const exportConfirmed = (project?.confirmed_stages || project?.checkpoint?.confirmed_stages || []).includes("export");
  const hasActiveJob = ["queued", "running", "processing", "paused"].includes(String(job?.status || "").toLowerCase());
  const pendingStage = project?.pending_confirmation_stage || project?.checkpoint?.pending_confirmation_stage;
  const completedStages = new Set(project?.checkpoint?.completed_stages || []);
  const confirmedStages = new Set(project?.confirmed_stages || project?.checkpoint?.confirmed_stages || []);
  const legacyReviewStage = !pendingStage && !hasActiveJob && orderedStages.find((item) => completedStages.has(item) && !confirmedStages.has(item));
  const currentStage = pendingStage || legacyReviewStage || (job?.stage === "done" ? "export" : job?.stage) || project?.current_stage || project?.stage || "probe";
  const issueMessage = legacyReviewStage
    ? "旧版结果先逐项复核；后续异常会在所属阶段处理。"
    : pendingStage
      ? "当前及前序阻断项会阻止确认；后续问题到对应阶段再处理。"
      : "异常在所属阶段处理；阻断项会锁住对应阶段确认。";

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
            <div className="project-copy"><span>当前项目</span><h2>{project.series_name || project.series?.name || "未命名系列"}</h2><h3>{project.title || project.name || `项目 ${project.id}`}</h3><p>{legacyReviewStage ? "旧版处理结果待逐项复核；确认前不会自动推进。" : job?.message || project.status_message || `当前状态：${statusText[project.status] || project.status || "等待真实处理"}`}</p></div>
            <div className="project-actions">{pendingStage ? <Button icon="chevron" onClick={jumpToReview}>查看并确认本阶段</Button> : <Button icon="play" onClick={continueProject} disabled={!data.health.connected || hasActiveJob}>{legacyReviewStage ? "开始逐项复核" : "继续任务"}</Button>}{hasActiveJob && <Button variant="secondary" icon="warning" onClick={cancelCurrent}>取消任务</Button>}<Button variant="secondary" icon="warning" onClick={() => onNavigate("exceptions")}>查看异常</Button></div>
          </div>
          <ProgressSteps project={project} jobs={data.jobs}/>
          <button className={`issue-strip ${issues ? "has-issues" : ""}`} onClick={() => onNavigate("exceptions")}>
            <Icon name="warning" size={21}/><span><strong>{issues ? `需要处理 ${issues} 项` : "暂无已报告异常"}</strong><small>{issues ? issueMessage : "不可测质量不会被伪装为已验证。"}</small></span><Icon name="chevron"/>
          </button>
          <StageReview project={project} stage={currentStage} job={job} data={data} onNavigate={onNavigate} legacyReview={Boolean(legacyReviewStage)}/>
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
          <div className="preview-actions"><Button variant="secondary" icon="play" disabled={!project || !previewSource}>试听</Button><Button icon="download" busy={exporting} onClick={exportProject} disabled={!project || !data.health.connected || !previewReady || !exportConfirmed}>{exportConfirmed ? "下载已验收 MP4" : "最终确认后可下载"}</Button></div>
          {downloadUrl && <a className="download-result" href={downloadUrl} download>下载已验收 MP4</a>}
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

function StageReview({ project, stage, job, data, onNavigate, legacyReview = false }) {
  const waiting = Boolean(project?.pending_confirmation_stage || project?.checkpoint?.pending_confirmation_stage);
  const failed = !legacyReview && ["blocked", "failed"].includes(String(job?.status || project?.status || "").toLowerCase());
  const confirmed = new Set(project?.confirmed_stages || project?.checkpoint?.confirmed_stages || []);
  const finished = ["completed", "completed_with_warnings"].includes(project?.status) && orderedStages.every((item) => confirmed.has(item));
  const anomalies = data.anomalies.filter((item) => !item.resolved);
  const currentIndex = orderedStages.indexOf(stage);
  const history = project?.confirmation_history || project?.checkpoint?.confirmation_history || [];
  const acceptedEarlierWarnings = new Set(history.filter((record) => record.valid !== false && orderedStages.indexOf(record.stage) < currentIndex).flatMap((record) => record.accepted_warning_ids || []));
  const blockers = anomalies.filter((item) => (item.blocking || item.severity === "blocking") && (!item.stage || orderedStages.indexOf(item.stage) <= currentIndex));
  const warnings = anomalies.filter((item) => !item.blocking && item.severity !== "blocking" && (!item.stage || item.stage === stage) && !acceptedEarlierWarnings.has(item.id));
  const segments = data.segments.filter((segment) => segment.kind === "dialogue");
  const subtitleSegments = segments.filter((segment) => String(segment.target_text || "").trim());
  const metadata = project?.metadata || {};
  const probe = metadata.probe || {};
  const separation = metadata.separation || {};
  const revision = Number(project?.pending_confirmation_revision || project?.checkpoint?.pending_confirmation_revision || 1);
  const nextStage = orderedStages[currentIndex + 1];
  const canRetry = failed && job?.id && stage !== "done";
  const finalStage = stage === "export";
  const missingAudioCount = stage === "synthesize"
    ? segments.filter((segment) => !segment.audio_url).length
    : stage === "subtitle"
      ? subtitleSegments.filter((segment) => !segment.audio_url).length
      : 0;
  const missingSubtitle = stage === "subtitle" && !data.preview?.subtitle_url;
  const reviewIncomplete = missingAudioCount > 0 || missingSubtitle;

  const confirm = () => data.act(() => api.confirmStage(project.id, stage, {
    revision,
    accepted_warning_ids: warnings.map((item) => item.id),
    ...(finalStage ? { artifact_sha256: data.preview?.output_sha256 || project.output_sha256 } : {}),
  }), finalStage ? "已确认当前候选成片。" : `已确认${stageNames[stage]}，正在进入${stageNames[nextStage]}。`);
  const retry = () => job?.id && data.act(() => api.retryJob(job.id, stage), "本阶段已重新排队；此前阶段结果保留。 ");

  if (!project) return null;
  const rows = stage === "subtitle" ? segments : segments.slice(0, 80);

  return <section className="stage-review" id="stage-review" aria-labelledby="stage-review-title">
    <div className="stage-review-head">
      <div><span className={`stage-review-badge ${waiting ? "is-waiting" : failed ? "is-blocked" : finished ? "is-finished" : ""}`}>{waiting ? "待你确认" : failed ? "需处理" : finished ? "已验收" : ["queued", "running", "processing"].includes(job?.status) ? "处理中" : legacyReview ? "待复核" : "当前阶段"}</span><h2 id="stage-review-title">{stageNames[stage] || stage}</h2><p>{waiting ? "阶段产物已保存。检查结果后，确认才会启动下一步。" : legacyReview ? "这是升级前已生成的阶段结果。开始复核后会从最早未确认项开始，复用有效缓存并逐关等待确认。" : failed ? job?.error?.message || job?.message || project.status_message : finished ? "所有阶段均已人工确认，当前输出可正式下载。" : job?.message || project.status_message || "确认前会停在此阶段，不会自动推进。"}</p></div>
      <span className="stage-review-count">第 {Math.max(1, currentIndex + 1)} / {orderedStages.length} 步</span>
    </div>

    <div className="stage-review-body">
      <div className="stage-result">
        {stage === "probe" && <dl className="stage-metadata"><div><dt>源文件</dt><dd>{project.source_filename || project.title || "—"}</dd></div><div><dt>时长</dt><dd>{Number(project.duration || probe.duration || 0).toFixed(2)} 秒</dd></div><div><dt>视频轨</dt><dd>{probe.video_streams ?? "未探测"}</dd></div><div><dt>音频轨</dt><dd>{probe.audio_streams ?? "未探测"}</dd></div></dl>}
        {stage === "separate" && <div className="stage-audio-grid">{[["对白轨", data.preview?.speech_url], ["背景轨", data.preview?.background_url], ["音乐轨", data.preview?.music_url], ["音效轨", data.preview?.effects_url]].map(([label, url]) => <div className="stage-audio-item" key={label}><strong>{label}</strong>{url ? <audio controls preload="none" src={resolveUrl(url)} aria-label={`${label}试听`}/> : <small>暂无可试听产物</small>}</div>)}<small className="stage-note">歌曲区间：{separation.song_intervals?.length ?? "尚未核实"} 段；歌曲检测状态：{separation.song_detection?.status || "未验证"}（原歌曲按设置保留）</small></div>}
        {["transcribe", "diarize", "rewrite", "synthesize"].includes(stage) && <SegmentReviewTable stage={stage} segments={rows} project={project}/>}
        {stage === "characters" && <div className="character-review-list">{data.characters.map((character) => <article key={character.id}><div><strong>{character.name || character.speaker_key}</strong><small>{character.speaker_key} · {character.status || "状态未知"}</small></div><span>{character.voice_profile || "尚无音色映射"}</span>{character.main_sample_url ? <audio controls preload="none" src={resolveUrl(character.main_sample_url)} aria-label={`${character.name}主样本试听`}/> : <small>无主样本</small>}</article>)}{!data.characters.length && <p className="quiet-empty">尚无角色档案；检查本阶段异常后再确认。</p>}</div>}
        {stage === "mix" && (data.preview?.mix_url ? <div className="mix-review"><strong>混音候选试听</strong><audio controls preload="none" src={resolveUrl(data.preview.mix_url)} aria-label="混音候选试听"/><small>请确认背景音乐、音效及原歌曲仍保留，人物对白替换符合预期。</small></div> : <p className="quiet-empty">混音轨尚未生成，不能确认。</p>)}
        {stage === "subtitle" && <>
          <div className="subtitle-review"><p>逐条核对原文、字幕时间和实际配音；成片只显示目标语言字幕，歌曲保留原音。</p>{data.preview?.subtitle_url ? <a href={resolveUrl(data.preview.subtitle_url)} download>下载 SRT 字幕文件</a> : <small>字幕文件尚未生成。</small>}</div>
          <SegmentReviewTable stage="subtitle" segments={rows.filter((item) => String(item.target_text || "").trim())} project={project}/>
        </>}
        {stage === "export" && (data.preview?.output_url ? <div className="candidate-review"><video controls preload="metadata" src={resolveUrl(data.preview.output_url)} aria-label="候选成片预览"/><small>这是待验收候选版。预览后点击“确认成片并完成”，之后才开放正式下载。</small><code>SHA-256: {data.preview.output_sha256 || project.output_sha256 || "未记录"}</code></div> : <p className="quiet-empty">候选成片尚未生成，不能最终确认。</p>)}
        {stage === "probe" && data.preview?.source_url && <video className="source-review-video" controls preload="metadata" src={resolveUrl(data.preview.source_url)} aria-label="源视频预览"/>}
        {!waiting && !failed && !legacyReview && <div className="stage-running-note" role="status">本阶段完成后会自动停住，等待你的确认。</div>}
      </div>

      <aside className="stage-checks">
        <h3>本关检查</h3>
        <p><strong>{stage === "characters" ? data.characters.length : segments.length}</strong> {stage === "characters" ? "个角色档案" : "条对白片段"}</p>
        <p className={blockers.length ? "check-blocked" : ""}><strong>{blockers.length}</strong> 项阻断异常</p>
        <p className={warnings.length ? "check-warning" : ""}><strong>{warnings.length}</strong> 项待接受警告</p>
        {reviewIncomplete && <p className="check-blocked" role="alert"><strong>暂不能确认：</strong>{missingSubtitle ? "字幕文件不可用。" : `${missingAudioCount} 条字幕没有对应的实际配音。`}</p>}
        {blockers.length > 0 && <ul className="stage-anomaly-list blocking">{blockers.map((item) => <li key={item.id}>{item.message || item.kind}</li>)}</ul>}
        {warnings.length > 0 && <ul className="stage-anomaly-list">{warnings.map((item) => <li key={item.id}>{item.message || item.kind}</li>)}</ul>}
        {stage === "subtitle" && <Button variant="ghost" onClick={() => onNavigate("series")}>编辑字幕 / 配音</Button>}
        {(blockers.length || warnings.length) ? <Button variant="secondary" onClick={() => onNavigate("exceptions")}>打开异常详情</Button> : null}
        {!["probe", "separate", "mix", "subtitle", "export"].includes(stage) && <Button variant="ghost" onClick={() => onNavigate("series")}>到项目页检查 / 编辑</Button>}
      </aside>
    </div>

    {waiting && <div className="stage-review-actions"><div><strong>{blockers.length ? "先解决阻断异常" : reviewIncomplete ? "结果不完整，暂不能确认" : warnings.length ? "确认表示你已查看并接受以上警告" : "本阶段结果将保留"}</strong><small>{blockers.length ? "阻断项解决并重新复核后才能继续。" : reviewIncomplete ? "先补齐字幕文件或对应配音，再进行人工复核。" : finalStage ? "最终确认会绑定当前候选文件的 SHA-256。" : `下一步：${stageNames[nextStage] || "完成"}`}</small></div><Button icon="refresh" variant="secondary" onClick={retry} disabled={!job?.id}>重新处理本阶段</Button><Button icon="chevron" onClick={confirm} disabled={blockers.length > 0 || reviewIncomplete || (finalStage && !data.preview?.output_sha256 && !project.output_sha256)}>{finalStage ? "确认成片并完成" : "确认并进入下一步"}</Button></div>}
    {legacyReview && <div className="stage-review-actions"><div><strong>已有结果需要人工复核</strong><small>只恢复第一个未确认阶段，不会重跑有效缓存，也不会越过本阶段确认。</small></div><Button icon="play" onClick={() => data.act(() => api.enqueue(project.id), "已开始逐项复核；第一个阶段结果生成后会停下等你确认。")}>开始逐项复核</Button></div>}
    {canRetry && <div className="stage-review-actions"><div><strong>本阶段未完成</strong><small>修复异常后重试本阶段；之前确认的上游结果会保留。</small></div><Button icon="refresh" onClick={retry}>重试当前阶段</Button></div>}
    {!waiting && !failed && !finished && ["created", "paused", "cancelled", "completed", "completed_with_warnings"].includes(project.status) && <div className="stage-review-actions"><div><strong>{project.status.startsWith("completed") ? "已有候选结果" : "尚未开始当前阶段"}</strong><small>继续后仍会在本阶段停下，等待你确认。</small></div><Button icon="play" onClick={() => data.act(() => api.enqueue(project.id), "任务已加入本地队列。")}>{project.status.startsWith("completed") ? "逐项复核已有结果" : "继续任务"}</Button></div>}
  </section>;
}

function SegmentReviewTable({ stage, segments, project }) {
  const reviewColumns = stage === "transcribe" ? ["时间", "原始识别文本", "分类"] : stage === "diarize" ? ["时间", "说话人", "原文"] : stage === "rewrite" ? ["原文", "目标文本", "语言等级"] : stage === "subtitle" ? ["字幕时间 / 角色", "原文参考 / 成片字幕", "对应配音试听"] : ["原文 / 目标文本", "角色与时间", "配音试听 / 时长差"];
  return <div className={`stage-segment-table ${stage}`} aria-label={`${stageNames[stage]}结果`}>
    <div className="stage-segment-header">{reviewColumns.map((column) => <strong key={column}>{column}</strong>)}</div>
    {segments.map((item) => <article key={item.id}>
      {stage === "transcribe" && <><span>{formatTime(item.start)}–{formatTime(item.end)}</span><p>{item.source_text || "—"}</p><span>{item.kind === "song" ? "歌曲（保留原音）" : "对白"}</span></>}
      {stage === "diarize" && <><span>{formatTime(item.start)}–{formatTime(item.end)}</span><strong>{item.speaker_name || item.speaker_key || "待分配"}</strong><p>{item.source_text || "—"}</p></>}
      {stage === "rewrite" && <><p>{item.source_text || "—"}</p><p>{item.target_text || "尚未改写"}</p><span>{project.level || project.target_level || "—"} · {project.target_language || "—"}</span></>}
      {stage === "subtitle" && <><div><span>{formatSubtitleTime(item.start)}–{formatSubtitleTime(item.end)}</span><small>{item.speaker_name || item.speaker_key || "未分配角色"}</small></div><div className="subtitle-comparison"><small>原文：{item.source_text || "—"}</small><strong>字幕：{item.target_text || "字幕文本缺失"}</strong></div><div className="stage-segment-audio">{item.audio_url ? <audio controls preload="none" src={resolveUrl(item.audio_url)} aria-label={`第 ${item.index + 1} 条配音试听`}/> : <small>没有可试听的实际配音</small>}</div></>}
      {stage === "synthesize" && <><div><p>{item.source_text || "—"}<br/><strong>{item.target_text || "尚无目标文本"}</strong></p></div><span>{item.speaker_name || item.speaker_key || "待分配"}<br/>{formatTime(item.start)}–{formatTime(item.end)}</span><div className="stage-segment-audio">{item.audio_url ? <audio controls preload="none" src={resolveUrl(item.audio_url)} aria-label={`片段 ${item.index + 1} 配音试听`}/> : <small>无可用配音</small>}<small>{item.duration_delta == null ? "未测时长" : `时长差 ${Number(item.duration_delta).toFixed(2)} 秒`}</small></div></>}
    </article>)}
    {!segments.length && <p className="quiet-empty">本阶段尚无可展示的对白结果。</p>}
    {segments.length > 80 && <p className="stage-note">当前显示前 80 条；完整时间轴可在“项目”页查看。</p>}
  </div>;
}

function formatTime(value) {
  const seconds = Number(value || 0);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function formatSubtitleTime(value) {
  const seconds = Math.max(0, Number(value || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = (seconds % 60).toFixed(3).padStart(6, "0");
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${remainder}`;
}
