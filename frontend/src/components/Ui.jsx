import { Icon } from "../icons";

export function Button({ children, icon, variant = "primary", busy = false, ...props }) {
  return <button className={`button ${variant}`} {...props}>{busy ? <span className="spinner"/> : icon && <Icon name={icon} size={18}/>}<span>{children}</span></button>;
}

export function ConnectionBanner({ health, onRetry }) {
  if (health.connected) return null;
  const checking = health.overall === "checking";
  return <section className="connection-banner" role={checking ? "status" : "alert"}>
    <Icon name="warning" size={21}/>
    <div><strong>{checking ? "正在检查本地后端" : "本地后端未连接"}</strong><p>{checking ? health.message : `${health.message} 演示状态不会标记任务完成。`}</p></div>
    <Button variant="ghost" icon="refresh" onClick={onRetry}>重新连接</Button>
  </section>;
}

export function EmptyState({ title, text, action }) {
  return <div className="empty-state"><span className="empty-symbol">＋</span><strong>{title}</strong><p>{text}</p>{action}</div>;
}

export function StatusDot({ value }) {
  const normalized = String(value || "unknown").toLowerCase();
  const tone = ["ok", "healthy", "ready", "connected", "completed", "success"].includes(normalized) ? "ok"
    : ["running", "processing", "queued", "configured"].includes(normalized) ? "active"
      : ["error", "failed", "missing", "offline", "unavailable", "blocked", "cancelled"].includes(normalized) ? "danger" : "muted";
  return <span className={`status-dot ${tone}`} aria-hidden="true"/>;
}

export function ProgressSteps({ project, jobs }) {
  const steps = [
    ["probe", "检查"], ["separate", "分离"], ["transcribe", "转写"], ["diarize", "角色"], ["characters", "音色"],
    ["rewrite", "改写"], ["synthesize", "配音"], ["mix", "混音"], ["subtitle", "字幕"], ["export", "输出"],
  ];
  const job = jobs.find((item) => String(item.project_id) === String(project?.id));
  const current = project?.current_stage || project?.stage || job?.stage || "probe";
  const index = Math.max(steps.findIndex(([id]) => id === current), 0);
  const complete = ["completed", "completed_with_warnings"].includes(project?.status) || job?.status === "completed";
  const progress = Math.max(0, Math.min(1, Number(job?.progress ?? project?.progress ?? 0)));
  return <ol className="progress-steps" aria-label="处理进度">
    {steps.map(([id, label], stepIndex) => {
      const state = complete || stepIndex < index ? "done" : stepIndex === Math.max(index, 0) ? "current" : "pending";
      return <li key={id} className={state}><span>{state === "done" ? "✓" : stepIndex + 1}</span><strong>{label}</strong><small>{state === "done" ? "已完成" : state === "current" ? `${Math.round(progress * 100)}%` : "等待中"}</small></li>;
    })}
  </ol>;
}

export function Toast({ notice, onClose }) {
  if (!notice) return null;
  return <div className={`toast ${notice.tone}`} role="status"><span>{notice.text}</span><button onClick={onClose} aria-label="关闭提示">×</button></div>;
}
