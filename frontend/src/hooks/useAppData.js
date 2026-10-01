import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";

const idleHealth = {
  connected: false,
  overall: "checking",
  app: {},
  services: {},
  capabilities: {},
  checked_at: null,
  message: "正在连接本地后端…",
};

function healthFrom(result) {
  const rawServices = result?.services || result?.dependencies || {};
  const overall = result?.overall || (result?.ok === false ? "degraded" : "healthy");
  return {
    connected: true,
    overall,
    app: result?.app || {},
    services: rawServices,
    capabilities: result?.capabilities || {},
    checked_at: result?.checked_at || null,
    message: result?.message || (overall === "healthy" ? "本地后端已连接" : "本地后端已连接，但部分能力不可用"),
    version: result?.app?.version || result?.version,
  };
}

function projectJobs(projects) {
  return projects.flatMap((project) => {
    const job = project.current_job || project.job;
    return job ? [{ ...job, project_id: job.project_id || project.id, project_title: job.project_title || project.title || project.name }] : [];
  });
}

function operationFailed(result) {
  const status = String(result?.status || result?.job?.status || "").toLowerCase();
  return ["blocked", "failed", "error"].includes(status);
}

export function useAppData() {
  const [health, setHealth] = useState(idleHealth);
  const [settings, setSettings] = useState(null);
  const [series, setSeries] = useState([]);
  const [projects, setProjects] = useState([]);
  const [projectDetail, setProjectDetail] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [segments, setSegments] = useState([]);
  const [anomalies, setAnomalies] = useState([]);
  const [characters, setCharacters] = useState([]);
  const [preview, setPreview] = useState(null);
  const [selectedProjectId, setSelectedProjectId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState(null);
  const requestId = useRef(0);
  const refreshing = useRef(false);
  const healthChecking = useRef(false);
  const healthOutcome = useRef("idle");
  const dataReadState = useRef("unknown");

  const refresh = useCallback(async ({ quiet = false } = {}) => {
    if (refreshing.current) return;
    refreshing.current = true;
    const current = ++requestId.current;
    dataReadState.current = "pending";
    if (!quiet) setLoading(true);
    if (!healthChecking.current) {
      healthChecking.current = true;
      healthOutcome.current = "pending";
      api.health().then((result) => {
        healthOutcome.current = "success";
        setHealth(healthFrom(result));
      }).catch((error) => {
        healthOutcome.current = "failed";
        setHealth((previous) => {
          if (dataReadState.current === "connected") {
            return { ...previous, connected: true, overall: "unknown", message: "项目与设置已连接，服务能力状态未知。" };
          }
          if (dataReadState.current === "pending") {
            return { ...previous, overall: previous.connected ? previous.overall : "checking", message: previous.connected ? previous.message : "健康检查未完成，正在确认项目与设置接口。" };
          }
          return { ...previous, connected: false, overall: "offline", message: error.message };
        });
      }).finally(() => {
        healthChecking.current = false;
      });
    }

    const results = await Promise.allSettled([
      api.listSeries(),
      api.listProjects(),
      api.settings(),
    ]);
    if (current !== requestId.current) return;

    const [seriesResult, projectsResult, settingsResult] = results;
    const nextSeries = seriesResult.status === "fulfilled" ? seriesResult.value : [];
    const nextProjects = projectsResult.status === "fulfilled" ? projectsResult.value : [];
    const nextJobs = projectJobs(nextProjects);
    dataReadState.current = projectsResult.status === "fulfilled" || settingsResult.status === "fulfilled" ? "connected" : "failed";
    if (dataReadState.current === "connected") {
      setHealth((previous) => {
        if (healthOutcome.current === "success" || (previous.connected && healthOutcome.current !== "failed")) return previous;
        return {
          ...previous,
          connected: true,
          overall: healthOutcome.current === "failed" ? "unknown" : "checking",
          message: healthOutcome.current === "failed" ? "项目与设置已连接，服务能力状态未知。" : "项目与设置已连接，正在检查服务能力。",
        };
      });
    } else if (healthOutcome.current === "failed") {
      setHealth((previous) => ({ ...previous, connected: false, overall: "offline" }));
    }
    setSeries(nextSeries);
    setProjects(nextProjects);
    setJobs(nextJobs);
    if (settingsResult.status === "fulfilled") setSettings(settingsResult.value);
    setSelectedProjectId((value) => nextProjects.some((item) => String(item.id) === String(value)) ? value : nextProjects[0]?.id || null);
    const failed = results.find((result) => result.status === "rejected");
    if (failed && !quiet) setNotice({ tone: "danger", text: `部分数据暂时无法读取：${failed.reason?.message || "请重试"}` });
    setLoading(false);
    refreshing.current = false;
  }, []);

  const recheck = useCallback(async () => {
    if (!health.connected) {
      await refresh();
      return null;
    }
    try {
      const result = await api.recheckSettings();
      setNotice({ tone: "success", text: "服务检查已完成，正在同步最新状态。" });
      await refresh({ quiet: true });
      return result;
    } catch (error) {
      setNotice({ tone: "danger", text: error.action ? `${error.message} ${error.action}` : error.message });
      return null;
    }
  }, [health.connected, refresh]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(() => refresh({ quiet: true }), 12000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const selectedProjectSummary = useMemo(
    () => projects.find((item) => String(item.id) === String(selectedProjectId)) || null,
    [projects, selectedProjectId],
  );

  useEffect(() => {
    if (!health.connected || !selectedProjectId) {
      setProjectDetail(null);
      setSegments([]);
      setAnomalies([]);
      setCharacters([]);
      setPreview(null);
      return undefined;
    }
    let cancelled = false;
    const seriesId = selectedProjectSummary?.series_id || selectedProjectSummary?.series?.id;
    Promise.allSettled([
      api.getProject(selectedProjectId),
      api.listSegments(selectedProjectId),
      api.listAnomalies(selectedProjectId),
      api.getPreview(selectedProjectId),
      seriesId ? api.listCharacters(seriesId) : Promise.resolve([]),
    ]).then((results) => {
      if (cancelled) return;
      const [projectResult, segmentResult, anomalyResult, previewResult, characterResult] = results;
      if (projectResult.status === "fulfilled") {
        const detail = projectResult.value || {};
        setProjectDetail(detail);
        const detailJob = detail.current_job ?? detail.job;
        if (detailJob) {
          setJobs((previous) => [
            ...previous.filter((item) => String(item.project_id) !== String(detail.id || selectedProjectId)),
            { ...detailJob, project_id: detailJob.project_id || detail.id || selectedProjectId, project_title: detailJob.project_title || detail.title || detail.name },
          ]);
        } else if (Object.prototype.hasOwnProperty.call(detail, "current_job") || Object.prototype.hasOwnProperty.call(detail, "job")) {
          setJobs((previous) => previous.filter((item) => String(item.project_id) !== String(detail.id || selectedProjectId)));
        }
      }
      if (segmentResult.status === "fulfilled") setSegments(segmentResult.value);
      else setSegments([]);
      if (anomalyResult.status === "fulfilled") setAnomalies(anomalyResult.value);
      else setAnomalies([]);
      if (previewResult.status === "fulfilled") setPreview(previewResult.value);
      else setPreview(null);
      if (characterResult.status === "fulfilled") setCharacters(characterResult.value);
      else setCharacters([]);
    });
    return () => { cancelled = true; };
  }, [health.connected, selectedProjectId, selectedProjectSummary?.series_id, selectedProjectSummary?.series?.id]);

  const selectedProject = useMemo(() => {
    if (!selectedProjectSummary) return null;
    return projectDetail && String(projectDetail.id) === String(selectedProjectId)
      ? { ...selectedProjectSummary, ...projectDetail }
      : selectedProjectSummary;
  }, [projectDetail, selectedProjectId, selectedProjectSummary]);

  const act = useCallback(async (operation, successText) => {
    if (!health.connected) {
      setNotice({ tone: "danger", text: "后端未连接，当前操作不会被伪装为完成。" });
      return null;
    }
    try {
      const result = await operation();
      if (operationFailed(result)) {
        setNotice({ tone: "danger", text: result?.message || result?.error?.message || "后端拒绝了这项操作。" });
      } else {
        setNotice({ tone: "success", text: successText });
      }
      await refresh({ quiet: true });
      return result;
    } catch (error) {
      setNotice({ tone: "danger", text: error.action ? `${error.message} ${error.action}` : error.message });
      return null;
    }
  }, [health.connected, refresh]);

  return {
    health, settings, series, projects, jobs, segments, setSegments, anomalies, characters, preview,
    selectedProject, selectedProjectId, setSelectedProjectId, loading, notice, setNotice, refresh, recheck, act,
  };
}
