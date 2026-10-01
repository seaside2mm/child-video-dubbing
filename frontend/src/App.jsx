import { useEffect, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { ConnectionBanner, Toast } from "./components/Ui";
import { ImportModal, NewSeriesModal } from "./components/Modals";
import { Dashboard } from "./views/Dashboard";
import { Projects } from "./views/Projects";
import { Processing } from "./views/Processing";
import { Exceptions } from "./views/Exceptions";
import { Settings } from "./views/Settings";
import { useAppData } from "./hooks/useAppData";

export default function App() {
  const data = useAppData();
  const [view, setView] = useState("projects");
  const [collapsed, setCollapsed] = useState(false);
  const [modal, setModal] = useState(null);

  useEffect(() => {
    const onKey = (event) => event.key === "Escape" && setModal(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const content = view === "projects" ? <Dashboard data={data} onNewSeries={() => setModal("series")} onImport={() => setModal("import")} onNavigate={setView}/>
    : view === "series" ? <Projects data={data} onImport={() => setModal("import")}/>
      : view === "processing" ? <Processing data={data}/>
        : view === "exceptions" ? <Exceptions data={data}/>
          : <Settings data={data}/>;

  return <div className={`app-shell ${collapsed ? "nav-collapsed" : ""}`}>
    <Sidebar active={view} onChange={setView} collapsed={collapsed} onToggle={() => setCollapsed((value) => !value)}/>
    <div className="app-main"><ConnectionBanner health={data.health} onRetry={() => data.refresh()}/>{content}</div>
    <footer className="status-bar"><span className={`live-dot ${data.health.connected ? "online" : ""}`}/><strong>{data.health.connected ? "本地服务运行中" : "本地服务未连接"}</strong><span>{data.selectedProject ? `当前项目：${data.selectedProject.series_name || "系列"} · ${data.selectedProject.title || data.selectedProject.name}` : "未选择项目"}</span><em>{data.loading ? "正在同步…" : "状态已同步"}</em></footer>
    <Toast notice={data.notice} onClose={() => data.setNotice(null)}/>
    {modal === "series" && <NewSeriesModal data={data} onClose={() => setModal(null)}/>} 
    {modal === "import" && <ImportModal data={data} onClose={() => setModal(null)}/>} 
  </div>;
}
