import { useRef, useState } from "react";
import { api } from "../api";
import { Button } from "./Ui";

const levelSpeeds = { L1: 0.82, L2: 0.86, L3: 0.9, L4: 0.94, L5: 1 };

export function NewSeriesModal({ data, onClose }) {
  const [form, setForm] = useState({ name: "", source_language: "auto", target_language: "zh-CN", level: "L1", speed: levelSpeeds.L1 });
  const [busy, setBusy] = useState(false);
  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    const result = await data.act(() => api.createSeries(form), "系列已建立。下一步可导入视频。");
    setBusy(false);
    if (result) onClose();
  };
  const updateLevel = (level) => setForm((value) => ({ ...value, level, speed: levelSpeeds[level] }));
  return <Modal title="新建系列" onClose={onClose}>
    <form className="modal-form" onSubmit={submit}>
      <label>系列名称<input required autoFocus value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="例如：小火车联盟"/></label>
      <div className="form-grid">
        <label>原语言<select value={form.source_language} onChange={(e) => setForm({ ...form, source_language: e.target.value })}><option value="auto">自动识别</option><option value="zh-CN">中文</option><option value="en-US">英文</option></select></label>
        <label>目标语言<select value={form.target_language} onChange={(e) => setForm({ ...form, target_language: e.target.value })}><option value="zh-CN">中文（简体）</option><option value="en-US">英文</option></select></label>
        <label>默认等级<select value={form.level} onChange={(e) => updateLevel(e.target.value)}>{[1, 2, 3, 4, 5].map((n) => <option key={n}>L{n}</option>)}</select></label>
        <label>默认语速<input type="number" min="0.55" max="1.15" step="0.01" value={form.speed} onChange={(e) => setForm({ ...form, speed: Number(e.target.value) })}/></label>
      </div>
      <p>L1–L5 是本工具的可配置档位；语速会随等级给出入门默认值，也可以手动调整。字幕只使用目标语言最终文本。</p>
      <div className="modal-actions"><Button type="button" variant="ghost" onClick={onClose}>取消</Button><Button type="submit" busy={busy}>建立系列</Button></div>
    </form>
  </Modal>;
}

export function ImportModal({ data, onClose }) {
  const [seriesId, setSeriesId] = useState(data.series[0]?.id || "");
  const [sourcePath, setSourcePath] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event) => {
    event.preventDefault();
    if (!sourcePath.trim() || !seriesId) return;
    setBusy(true);
    const result = await data.act(() => api.importProject({ sourcePath: sourcePath.trim(), seriesId, title: title.trim() }), "视频已登记并加入检查流程；源文件不会被覆盖。");
    setBusy(false);
    if (result) onClose();
  };
  return <Modal title="导入视频" onClose={onClose}>
    <form className="modal-form" onSubmit={submit}>
      <label>所属系列<select required value={seriesId} onChange={(e) => setSeriesId(e.target.value)}><option value="" disabled>请选择系列</option>{data.series.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>视频绝对路径<input required value={sourcePath} onChange={(e) => setSourcePath(e.target.value)} placeholder={'C:\\Videos\\example.mp4'}/></label>
      <label>项目名称（可选）<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="默认使用文件名"/></label>
      <p>本地网页不会把文件选择器的完整路径交给后端，因此请粘贴 Windows 绝对路径。后端只读校验并在项目缓存中复制，不移动或覆盖源文件，也不会上传整段视频。</p>
      <div className="modal-actions"><Button type="button" variant="ghost" onClick={onClose}>取消</Button><Button type="submit" busy={busy} disabled={!seriesId || !sourcePath.trim()}>登记并检查</Button></div>
    </form>
  </Modal>;
}

function Modal({ title, onClose, children }) {
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><header><h2 id="modal-title">{title}</h2><button type="button" onClick={onClose} aria-label="关闭">×</button></header>{children}</section></div>;
}
