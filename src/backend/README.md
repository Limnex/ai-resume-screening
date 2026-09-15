# 后端运行说明

## 这版后端做什么

- 使用 FastAPI 提供 `/api/v1` 接口；
- 默认使用不联网的 `DemoLLM` 跑通完整流程；关闭演示模式后可调用兼容 OpenAI `/chat/completions` 的真实大模型；
- 不使用数据库，任务数据保存在 `src/backend/runtime/jobs/` 的 JSON/JSONL 文件中；
- CSV 中岗位必须明确选择，不支持“全部岗位”；
- HR 可在前端设置本轮按 CSV 顺序处理 1～20 份简历，响应明确返回导入、选择、成功、失败和剩余数量；
- CSV 先通过后端预检接口生成短期 `preview_id`，前端不再复制解析和业务校验逻辑；
- 生产判断只使用原始完整 JD 和原始 `resume_text`，CSV 预提取字段单独保存为离线参考；
- 保存原始 AI 建议、证据校验结果、HR 复核和审计事件。

## 一键启动（推荐）

直接双击项目根目录的 `start.cmd`，或者在 PowerShell 中运行：

```powershell
.\start.cmd
```

脚本会自动完成：

1. 首次创建 `.venv`；
2. 缺少依赖时自动安装；
3. 缺少 `.env` 时从示例生成离线演示配置；
4. 检查演示模式或真实模型配置；
5. 启动服务并自动打开前端页面。

回到启动窗口按 `Ctrl+C` 可以停止程序。只检查环境而不启动服务时运行：

```powershell
.\start.cmd --check
```

## 首次安装（手动方式）

在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\src\backend\requirements.txt
Copy-Item .env.example .env
```

默认配置无需 API Key。需要接入真实模型时，打开项目根目录的 `.env` 并填写：

```env
DEMO_MODE=false
LLM_API_KEY=真实密钥
LLM_BASE_URL=https://模型服务地址/v1
LLM_MODEL=模型名称
```

不要把 `.env` 或真实密钥提交到 Git，也不要将密钥粘贴到截图、日志和聊天记录中。

## 启动（手动方式）

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.backend.main:app --host 127.0.0.1 --port 8000 --workers 1
```

启动后可以访问：

- 前端：`http://127.0.0.1:8000/frontend/index.html`
- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`

必须保持 `--workers 1`。普通文件存储像一个只有一名管理员的档案室，多进程同时写入可能相互覆盖。

## 自动化测试

测试使用模拟模型响应，不调用外部模型、不会产生调用费用：

```powershell
.\.venv\Scripts\python.exe -m pytest .\src\backend\tests -q --basetemp .pytest-tmp
```

测试覆盖：岗位必选、原始 JD 完整性、生产/参考数据隔离、UI 可配置数量、语义关联、数字年限、双指标分层、低匹配淘汰、有限推断、无效证据降级、失败计数守恒、复核理由、版本校验、幂等提交、分析过期和审计追溯。

首次执行浏览器联调前安装 Chromium：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

浏览器冒烟测试用 1 份测试简历走完创建任务、提取条件、分析、查看详情和 HR 复核流程；在默认配置下不会调用外部模型：

```powershell
.\.venv\Scripts\python.exe .\src\frontend\tests\e2e_smoke.py
```

## 运行数据

每个任务形成一个独立目录：

```text
src/backend/runtime/jobs/{job_id}/
├── job.json
├── resumes.jsonl
├── benchmark-reference.jsonl
├── criteria/
│   ├── draft.json
│   └── version-1.json
├── analyses/
│   ├── analysis-xxx-meta.json
│   └── analysis-xxx.jsonl
├── reviews.jsonl
├── audit.jsonl
└── idempotency.json
```

`runtime/` 已加入 `.gitignore`。删除该目录会删除所有本地任务记录，无法恢复。

应用代码按模块化单体组织：`application/` 保存任务、条件、分析和复核用例，`domain/` 保存纯业务规则，`ports.py` 定义仓储与模型端口，`storage.py` 和 `llm.py` 是可替换适配器。API 层只负责路由、契约验证和错误映射。

## 主要接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/import-previews` | 后端解析 CSV，返回岗位、数量和样例预览 |
| POST | `/api/v1/screening-jobs` | 上传 CSV 并创建任务 |
| GET | `/api/v1/screening-jobs/{job_id}` | 查询任务详情 |
| POST | `/api/v1/screening-jobs/{job_id}/criteria/extract` | 调用真实模型提取条件 |
| GET | `/api/v1/screening-jobs/{job_id}/criteria` | 获取条件草稿或当前版本 |
| PUT | `/api/v1/screening-jobs/{job_id}/criteria` | HR 确认并生成条件版本 |
| POST | `/api/v1/screening-jobs/{job_id}/analysis` | 按 UI 设置启动 1～20 份简历分析 |
| GET | `/api/v1/screening-jobs/{job_id}/analysis/status` | 查询进度 |
| GET | `/api/v1/screening-jobs/{job_id}/results` | 查询分层结果 |
| GET | `/api/v1/screening-jobs/{job_id}/candidates/{resume_id}` | 查询候选人详情与证据 |
| POST | `/api/v1/screening-jobs/{job_id}/reviews` | 提交 HR 复核 |
| GET | `/api/v1/screening-jobs/{job_id}/audit-trail` | 查询追溯记录 |

## 真实模型保护规则

- 只发送原始完整 JD、HR 确认条件、`resume_id` 和原始 `resume_text`；CSV 预提取的技能、年限、项目、证书、学历和测试标签均不会发送；
- Prompt 明确禁止学历、学校、性别、年龄、姓名等非岗位因素参与判断；
- 模型输出必须符合固定 JSON 结构；
- 匹配由模型做语义判断，不按字符覆盖率判断技能；直接证据必须能在原始 `resume_text` 中定位；
- 无法定位的证据会被降级为“信息不足”，候选人进入待定；
- 模型声称“不满足”但没有明确反证时，也会被改为“信息不足”；
- 匹配程度与证据可靠程度分别保存为 `match_score` 和 `evidence_confidence`；
- 推荐只使用双指标门槛：匹配分至少 75，证据可信度至少 0.7；不确定点、风险和部分匹配继续展示，但不再强制进入待定；
- 单份失败会显示错误代码与原因，并强制校验“已处理 = 成功 + 失败、结果数 = 成功数”；
- HR 的最终结果以新的复核事件保存，不覆盖原始 AI 建议。

演示环境默认 `SERVE_FRONTEND=true`，独立部署前端时可设为 `false`，并通过 `CORS_ORIGINS` 配置允许的前端来源。
