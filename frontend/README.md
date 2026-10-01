# 童声配音台前端

React + Vite 中文本地界面。所有任务、进度、异常和服务状态均来自本地后端；后端不可达时明确显示“未连接”，不会用前端假数据标记完成。

## 开发运行

```powershell
cd frontend
npm install
npm run dev
```

开发服务器为 `http://127.0.0.1:5173`，本地后端使用 `http://127.0.0.1:8787`。

## 构建与测试

```powershell
npm test
npm run build
```

生产构建输出在 `frontend/dist`。Windows 日常运行使用项目根目录的 `launcher/启动童声配音台.bat`；首次使用运行 `launcher/首次安装.bat`。

## 最小后端契约

- `GET /api/health`
- `GET /api/settings` / `POST /api/settings/recheck`
- `GET|POST /api/series`
- `GET /api/projects`
- `POST /api/series/{series_id}/projects`（JSON：`source_path`, `title`；源文件只读校验并复制到项目缓存）
- `POST /api/projects/{project_id}/jobs`（JSON：`kind`, `from_stage`, `force`）
- `GET /api/jobs/{job_id}` / `POST /api/jobs/{job_id}/cancel` / `POST /api/jobs/{job_id}/retry`
- `GET /api/projects/{id}/segments`
- `GET /api/projects/{id}/anomalies`
- `PATCH /api/projects/{id}/segments/{segment_id}`
- `POST /api/projects/{id}/segments/{segment_id}/regenerate`
- `GET /api/projects/{id}/preview`（JSON 媒体元数据）
- `POST /api/projects/{id}/export`（JSON：`burn_subtitles`）

生产环境默认同源请求；如需其他地址，只在构建时设置 `VITE_API_BASE`。密钥不得进入该变量或任何前端文件。
