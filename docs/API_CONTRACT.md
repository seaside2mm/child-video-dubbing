# 儿童视频分级配音工具 API 契约

本契约是前后端之间的最小稳定边界。所有路径在本地服务根下以 `/api` 开头；JSON 使用 UTF-8。后端不把密钥、请求头、远程服务返回的密钥或完整环境变量返回给浏览器。

## 状态约定

- `series.status`: `ready | blocked | error`
- `project.status`: `created | queued | processing | paused | completed | completed_with_warnings | blocked | failed`
- `job.status`: `queued | running | paused | completed | completed_with_warnings | blocked | failed | cancelled`
- `job.stage`: `probe | separate | transcribe | diarize | characters | rewrite | synthesize | mix | subtitle | export | done`
- `segment.status`: `pending | rewritten | synthesized | failed | blocked`
- `anomaly.severity`: `info | warning | blocking`

`blocked` 与 `failed` 都不是成功。只要没有真实外部服务或真实媒体产物，后端不能写入 `completed`。

## 通用响应

错误：

```json
{
  "error": {
    "code": "SERVICE_UNAVAILABLE",
    "message": "faster-whisper 服务不可达",
    "action": "启动服务或在设置中修正地址",
    "details": {}
  }
}
```

时间使用 ISO-8601 UTC 字符串；ID 使用本地生成的 `series-...`、`project-...`、`job-...`、`segment-...`。

## 健康与设置

### `GET /api/health`

返回本地依赖与已配置服务的可测状态。`overall` 为 `healthy | degraded | blocked`；服务检查不代表完整任务可完成。

```json
{
  "overall": "degraded",
  "app": {"version": "0.1.0", "database": "ok", "ffmpeg": "ok"},
  "services": {
    "faster_whisper": {"status": "ok", "base_url": "http://127.0.0.1:8001", "detail": "..."},
    "omnivoice": {"status": "blocked", "base_url": "http://127.0.0.1:7861", "detail": "..."},
    "text_rewriter": {"status": "configured", "base_url": "...", "model": "..."},
    "bandit": {"status": "missing", "detail": "..."},
    "diarization": {"status": "missing", "detail": "..."}
  },
  "capabilities": {
    "real_transcription": true,
    "real_voice_generation": false,
    "real_separation": false,
    "real_diarization": false,
    "demo_mode": false
  },
  "checked_at": "2026-09-15T00:00:00Z"
}
```

### `GET /api/settings`

只返回非敏感配置是否存在、等级规则与语速默认值，不返回密钥。

### `POST /api/settings/recheck`

重新检查本地依赖与配置，响应同 `GET /api/health`。

## 系列与项目

### `GET /api/series`

返回系列摘要数组：`id, name, source_language, target_language, level, speed, project_count, updated_at, status`。

### `POST /api/series`

请求：

```json
{
  "name": "小火车联盟",
  "source_language": "auto",
  "target_language": "zh-CN",
  "level": "L1",
  "speed": 0.85
}
```

`level` 只能为 `L1`–`L5`；`speed` 范围为 `0.55`–`1.15`。响应为完整系列对象。

### `GET /api/series/{series_id}`

返回完整系列及其项目和持久化角色库摘要。

### `GET /api/projects`

可选查询 `series_id`、`status`；返回项目摘要数组。

### `POST /api/series/{series_id}/projects`

请求：

```json
{
  "source_path": "Z:\\000_child_edu\\小火车联盟\\videos\\episode-01.mp4",
  "title": "第 01 集"
}
```

后端只读校验源文件、计算 SHA-256、探测媒体，并在本项目 `work/projects/{project_id}` 建立缓存；绝不覆盖或移动源文件。响应包含 `source_path, source_sha256, duration, status, work_dir`。

### `GET /api/projects/{project_id}`

返回完整项目摘要、当前 job、段数、异常数、输出文件元数据和可用媒体 URL。

## 任务与恢复

### `POST /api/projects/{project_id}/jobs`

请求可省略或传：

```json
{"kind": "process", "from_stage": "auto", "force": false}
```

后端按持久化 checkpoint 恢复；`force=true` 只使当前项目相关阶段失效，不删除源文件。响应 `{job_id, status, stage}`。

### `GET /api/jobs/{job_id}`

返回：

```json
{
  "id": "job-...",
  "project_id": "project-...",
  "status": "running",
  "stage": "rewrite",
  "progress": 0.46,
  "message": "正在按场景改写 8 条对白",
  "checkpoint": {"completed_stages": ["probe", "separate", "transcribe", "diarize"]},
  "error": null,
  "created_at": "...",
  "updated_at": "..."
}
```

### `POST /api/jobs/{job_id}/cancel`

请求取消可取消阶段；已生成的缓存保留，job 状态为 `cancelled`。

### `POST /api/jobs/{job_id}/retry`

从最近一个可恢复 checkpoint 重试，或使用 `{ "from_stage": "rewrite" }` 指定阶段。旧 job 保留审计记录。

## 片段、异常与媒体

### `GET /api/projects/{project_id}/segments`

返回按原时间排序的句级段数组：

```json
{
  "id": "segment-...",
  "index": 0,
  "start": 10.5,
  "end": 12.9,
  "speaker_key": "cluster-0",
  "speaker_name": "角色 A",
  "kind": "dialogue",
  "source_text": "Let's go, everybody!",
  "target_text": "我们出发吧，大家！",
  "target_language": "zh-CN",
  "speed": 0.85,
  "duration_delta": 0.3,
  "voice_profile": "series-character-...",
  "status": "synthesized",
  "audio_url": "/api/projects/project-.../media/segment/segment-...",
  "updated_at": "..."
}
```

### `PATCH /api/projects/{project_id}/segments/{segment_id}`

可编辑 `target_text, speed, speaker_key`。任一修改都会增加该片段的 revision，并使相关片段音频、混音、字幕和成片失效；不会重做无关片段的转写/分离或音频。

### `POST /api/projects/{project_id}/segments/{segment_id}/regenerate`

只重新执行该片段的真实改写/配音和依赖产物检查；缺服务时返回 `blocked`，不得返回旧缓存冒充新结果。

### `GET /api/projects/{project_id}/anomalies`

返回异常数组，字段为 `id, segment_id, kind, severity, blocking, message, action, resolved, details`。

### `GET /api/projects/{project_id}/media/{kind}`

`kind` 支持 `source | output | segment/{segment_id} | subtitle`。只允许访问当前项目目录内的已登记文件；不存在或未完成时返回 404/409。

### `GET /api/projects/{project_id}/preview`

返回 `{source_url, output_url, duration, subtitle_url, ready, warnings}`，供网页播放器使用。

### `POST /api/projects/{project_id}/export`

请求可传 `{ "burn_subtitles": true }`。只在所有阻断异常解决且真实成片存在时返回候选文件信息；否则返回 409，明确阻断原因。

## 角色库

### `GET /api/series/{series_id}/characters`

返回跨集持久化角色：`id, speaker_key, name, main_sample_url, backup_sample_url, voice_profile, status, source_project_id`。名字可编辑；`speaker_key` 与样本/声纹映射不可由每集随机编号替代。

### `PATCH /api/series/{series_id}/characters/{character_id}`

更新名称、主/备用样本选择或已验证 voice profile；修改映射会使关联片段的配音与成片失效。

## 明确非契约行为

- `/api/demo` 不存在；演示数据不会被标为完成。
- 前端不能发送密钥，后端也不会把密钥放在响应、日志、项目清单或导出包。
- 远程服务只接收必要的文本/音频参考；不会擅自上传整段视频。
- `speaker_key` 只有在真实 diarization/embedding 或用户明确导入映射后才可用于跨集角色匹配。
