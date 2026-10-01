import { Button, StatusDot } from "../components/Ui";

const serviceLabels = { transcription: "faster-whisper 转写", faster_whisper: "faster-whisper 转写", dubbing: "Omni Voice 配音", omnivoice: "Omni Voice 配音", rewrite: "翻译与改写", text_rewriter: "翻译与改写", separation: "BandIt 对白分离", bandit: "BandIt 对白分离", diarization: "角色聚类" };
const defaultLevels = [["L1", "入门", 0.82], ["L2", "基础", 0.86], ["L3", "进阶", 0.9], ["L4", "熟练", 0.94], ["L5", "自然", 1]];

export function Settings({ data }) {
  const services = Object.entries(data.health.services || {});
  const capabilities = Object.entries(data.health.capabilities || {});
  const configuredRules = data.settings?.levels || data.settings?.level_rules;
  const rules = Array.isArray(configuredRules)
    ? configuredRules
    : configuredRules && typeof configuredRules === "object"
      ? Object.entries(configuredRules).map(([level, value]) => typeof value === "string" ? { level, rule: value } : { level, ...(value || {}) })
      : defaultLevels.map(([level, label, speed]) => ({ level, label, speed }));
  return <div className="standard-view"><header className="page-heading"><div><h1>设置</h1><p>只显示密钥是否已配置，不回显值；服务能力以实时健康检查为准。</p></div><Button variant="secondary" icon="refresh" onClick={() => data.recheck()}>重新检查</Button></header>
    <div className="settings-grid">
      <section className="panel"><h2>服务状态</h2>{services.length ? services.map(([key, service]) => { const value = service?.status || service; return <div className="setting-row" key={key}><div><strong>{serviceLabels[key] || service?.name || key}</strong><small>{service?.endpoint || service?.base_url || service?.detail || service?.message || "本地服务"}</small></div><span><StatusDot value={value}/>{service?.label || serviceText(value)}</span></div>; }) : <div className="setting-row"><div><strong>本地后端</strong><small>{data.health.overall === "checking" ? "服务能力检查中" : "健康检查尚未返回服务列表"}</small></div><span><StatusDot value={data.health.connected ? data.health.overall : "offline"}/>{data.health.connected || data.health.overall === "checking" ? serviceText(data.health.overall) : "未连接"}</span></div>}</section>
      <section className="panel"><h2>分级与语速</h2><p className="settings-copy">L1–L5 是可配置的产品档位，不等同于正式 CEFR 或识字考试认证。生成文本会同时考虑词汇、句型、原时间窗口和目标语速；字幕只使用目标语言最终文本。</p><dl className="level-list">{rules.map((item, index) => { const level = Array.isArray(item) ? item[0] : item.level || `L${index + 1}`; const label = Array.isArray(item) ? item[1] : item.label || item.name || item.rule || ""; const speed = Array.isArray(item) ? item[2] : item.speed ?? item.default_speed; return <div key={level}><dt>{level}</dt><dd>{label}{speed != null ? ` · ${Number(speed).toFixed(2)}×` : ""}</dd></div>; })}</dl></section>
      <section className="panel"><h2>能力开关</h2>{capabilities.length ? capabilities.map(([key, value]) => <div className="setting-row" key={key}><div><strong>{capabilityLabels[key] || key}</strong><small>由后端健康检查报告，前端不推断可用性</small></div><span><StatusDot value={value ? "ok" : "blocked"}/>{value ? "可用" : "不可用"}</span></div>) : <p className="quiet-empty">后端尚未返回能力状态。</p>}</section>
      <section className="panel"><h2>应用与健康</h2><div className="setting-row"><div><strong>总体状态</strong><small>{data.health.checked_at ? `检查于 ${new Date(data.health.checked_at).toLocaleString("zh-CN")}` : data.health.overall === "checking" ? "服务能力检查中" : "尚未检查"}</small></div><span><StatusDot value={data.health.overall}/>{data.health.connected || data.health.overall === "checking" ? serviceText(data.health.overall) : "未连接"}</span></div><div className="setting-row"><div><strong>数据库 / FFmpeg</strong><small>由本地后端提供</small></div><span>{data.health.app.database || "—"} / {data.health.app.ffmpeg || "—"}</span></div><div className="setting-row"><div><strong>版本</strong><small>仅用于本地诊断</small></div><span>{data.health.version || data.health.app.version || "—"}</span></div></section>
      <section className="panel full"><h2>质量边界</h2><div className="quality-grid"><div><strong>自动可测</strong><p>音频是否可解码、静音、实际时长、字幕文本一致、文件是否生成。</p></div><div><strong>保守告警</strong><p>歌曲区间、多人重叠、样本过短、分离伪影、角色映射不确定。</p></div><div><strong>不能保证</strong><p>语义绝对不变、音色完全相似、原对白百分百无残留。</p></div></div></section>
    </div>
  </div>;
}

const capabilityLabels = { real_transcription: "真实转写", real_voice_generation: "真实配音", real_separation: "真实对白分离", real_diarization: "真实角色聚类", demo_mode: "演示模式" };
function serviceText(value) { return { ok: "服务正常", healthy: "服务正常", ready: "服务正常", configured: "已配置", checking: "检查中", unknown: "未知", degraded: "部分能力不可用", blocked: "被阻断", missing: "缺少依赖", offline: "未连接", error: "检查失败" }[value] || "尚未核实"; }
