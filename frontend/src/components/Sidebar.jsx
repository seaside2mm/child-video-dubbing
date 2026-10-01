import { Icon } from "../icons";

const items = [
  ["series", "系列", "series"],
  ["projects", "项目", "project"],
  ["processing", "自动处理", "process"],
  ["exceptions", "异常", "warning"],
  ["settings", "设置", "settings"],
];

export function Sidebar({ active, onChange, collapsed, onToggle }) {
  return <aside className={`sidebar ${collapsed ? "is-collapsed" : ""}`}>
    <div className="brand">
      <span className="brand-mark" aria-hidden="true"><i/><i/><i/></span>
      <span className="brand-copy"><strong>童声配音台</strong><small>本地视频工作台</small></span>
    </div>
    <nav aria-label="主导航">
      {items.map(([id, label, icon]) => <button key={id} className={active === id ? "active" : ""} onClick={() => onChange(id)} title={label}>
        <Icon name={icon}/><span>{label}</span>
      </button>)}
    </nav>
    <button className="collapse-button" onClick={onToggle} aria-label={collapsed ? "展开导航" : "收起导航"}>
      <span>{collapsed ? "»" : "«"}</span><em>{collapsed ? "" : "收起导航"}</em>
    </button>
  </aside>;
}
