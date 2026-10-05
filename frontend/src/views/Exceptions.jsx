import { useMemo, useState } from "react";
import { Button, EmptyState, StatusDot } from "../components/Ui";

export function Exceptions({ data }) {
  const [onlyBlocking, setOnlyBlocking] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  const items = useMemo(() => data.anomalies.filter((item) => (showResolved || !item.resolved) && (!onlyBlocking || item.blocking || item.severity === "blocking")), [data.anomalies, onlyBlocking, showResolved]);
  return <div className="standard-view"><header className="page-heading"><div><h1>异常</h1><p>这里只显示后端登记的真实异常；没有异常记录不代表语义、音色相似度或原对白残留已被完全验证。</p></div><div className="exception-filters"><label className="switch"><input type="checkbox" checked={onlyBlocking} onChange={(event) => setOnlyBlocking(event.target.checked)}/><span/>仅看阻断项</label><label className="switch"><input type="checkbox" checked={showResolved} onChange={(event) => setShowResolved(event.target.checked)}/><span/>显示已处理</label></div></header>
    <section className="exception-list panel">{items.map((item) => <article key={item.id}><div><StatusDot value={item.severity}/><strong>{item.kind || "需要确认"}</strong><span>{item.blocking || item.severity === "blocking" ? "阻断处理" : item.resolved ? "已处理" : "保守告警"}</span></div><h2>{item.speaker_name || item.character_name || item.speaker_key || (item.segment_id ? "片段异常" : "项目级异常")}{item.segment_id ? ` · ${item.segment_id}` : ""}</h2><p>{item.message || "该项目需要人工检查。"}</p><small>{item.action ? `建议：${item.action}` : item.details ? JSON.stringify(item.details) : "暂无额外操作说明"}</small></article>)}{!items.length && <EmptyState title="没有当前筛选范围内的异常" text={data.selectedProject ? "当前项目尚无后端登记的异常记录。" : "请选择项目后读取真实异常。"} action={onlyBlocking || showResolved ? <Button variant="secondary" onClick={() => { setOnlyBlocking(false); setShowResolved(false); }}>显示未处理全部异常</Button> : null}/>}</section>
  </div>;
}
