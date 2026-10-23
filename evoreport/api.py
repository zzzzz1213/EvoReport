from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .evolution import EvolutionService
from .agent_runtime import DailyQueueFull, DailyQueueTimeout, MultiAgentRuntime
from .llm import ArkReportWriter, ModelCallTimeout
from .role_executor import RoleProcessTimeout
from .git_activity import GitActivityError, collect_activity, validate_directory_path, validate_repository_path
from .report_generator import ReportStrategy, generate_daily, generate_weekly, redact_sensitive, source_fact_records
from .store import Store
from .docx_export import export_report_docx
from .evaluation import structured_claim
from .work_summary import parse_work_summary


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=500)
    project_type: Literal["git", "plain"] = "git"


class DailyCreate(BaseModel):
    report_date: date
    project_ids: List[str] = Field(default_factory=list)
    completed: str = Field(default="", max_length=10000)
    problems: str = Field(default="", max_length=10000)
    tomorrow: str = Field(default="", max_length=10000)


class QuickDailyCreate(BaseModel):
    report_date: date
    problems: str = Field(default="", max_length=10000)
    tomorrow: str = Field(default="", max_length=10000)


class WorkNoteCreate(BaseModel):
    note_date: date
    content: str = Field(min_length=1, max_length=1000)
    project_id: str | None = None


class WorkSummaryParse(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class WriterPromptCreate(BaseModel):
    addendum: str = Field(min_length=1, max_length=500)


class HistoricalReportCreate(BaseModel):
    report_date: date
    content: str = Field(min_length=1, max_length=100000)
    facts: List[str] = Field(default_factory=list, max_length=100)


SYNTHETIC_DAILY_SAMPLES = [
    ("2026-07-03", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 完成会员中心的订单列表筛选功能，支持按状态和时间范围查询，并补齐空状态提示。
2. 修复移动端筛选抽屉关闭后页面仍无法滚动的问题，已在常用手机尺寸下验证。
3. 与后端联调订单导出接口，补充失败提示和重复点击保护。

## 遇到的问题

- 测试环境中导出文件名乱码，已统一响应头编码，等待联调环境复测。

## 明日计划

- 完成订单详情页的售后信息展示并提交评审。"""),
    ("2026-07-04", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 排查登录页偶发白屏，定位为接口超时后加载状态未释放，补充超时兜底与重试入口。
2. 为核心登录流程补充 6 条测试用例，覆盖密码错误、验证码失效和网络异常。
3. 梳理线上错误日志，输出高频问题清单并同步给产品同学。

## 明日计划

- 根据清单处理账号冻结提示文案。"""),
    ("2026-07-05", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 将设计稿中的 12 个公共组件整理为统一样式变量，减少重复颜色和间距定义。
2. 完成消息通知页面的未读角标、批量已读和分页加载。
3. 进行代码评审，指出列表页存在的空值渲染和请求取消问题，并协助修复。

## 明日计划

- 整理本周组件改造的使用说明。"""),
    ("2026-07-06", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 为运营后台新增活动配置表单，包含表单校验、草稿保存和预览能力。
2. 解决图片上传后偶发顺序错乱的问题，改为按上传队列回填结果。
3. 和测试同学确认 9 个验收场景，其中 7 个已通过，2 个待接口字段补齐。

## 明日计划

- 等待字段补齐后完成剩余两个场景的回归。"""),
    ("2026-07-07", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 完成数据看板首版，展示新增用户、转化率和渠道分布三个核心指标。
2. 处理图表在无数据时显示异常的问题，增加空数据说明与加载骨架。
3. 将接口请求集中到统一模块，补齐错误提示和请求参数类型。

## 明日计划

- 根据产品反馈调整指标说明与默认时间范围。"""),
    ("2026-07-08", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 支持用户在个人设置页修改头像和昵称，完成前后端联调及权限校验。
2. 修复保存后头像缓存未刷新的问题，增加版本参数并验证刷新逻辑。
3. 编写发布检查清单，包含配置核对、数据库迁移和回滚步骤。

## 明日计划

- 跟进预发布环境的完整验收。"""),
    ("2026-07-09", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 在预发布环境完成个人设置功能验收，修复 2 个边界问题并重新部署。
2. 优化接口错误信息，避免直接向页面暴露服务端技术细节。
3. 协助整理本次上线说明，明确影响范围、验证结果和回滚负责人。

## 明日计划

- 观察上线后的错误告警，并处理反馈。"""),
    ("2026-07-10", """# 模拟日报（仅用于体验，不是个人真实经历）

## 今日完成

1. 观察上线后核心接口稳定，无新增异常告警；根据反馈微调头像裁剪提示。
2. 复盘本周任务，整理需求澄清、联调、验收三个阶段可复用的检查项。
3. 更新项目 README，补充本地启动、测试和发布前检查说明。

## 明日计划

- 开始下一轮消息中心需求的技术预研。"""),
]


class WeeklyCreate(BaseModel):
    week_start: date


class ConfirmRequest(BaseModel):
    final_text: str = Field(min_length=1, max_length=100000)
    acknowledge_warnings: bool = False


class FinalReviewResumeRequest(BaseModel):
    final_text: str = Field(min_length=1, max_length=100000)


class DraftUpdate(BaseModel):
    draft: str = Field(max_length=100000)


class ReviewClaimLabel(BaseModel):
    index: int = Field(ge=0)
    label: Literal["supported", "unsupported"]
    scenarios: List[Literal["compound", "paraphrase", "implicit"]] = Field(default_factory=list, max_length=3)


class MissedReviewClaim(BaseModel):
    line: int = Field(ge=1)
    column_start: int = Field(ge=0)
    column_end: int = Field(gt=0)
    scenarios: List[Literal["compound", "paraphrase", "implicit"]] = Field(default_factory=list, max_length=3)


class ReviewLabelsRequest(BaseModel):
    labels: List[ReviewClaimLabel]
    missed_claims: List[MissedReviewClaim] = Field(default_factory=list, max_length=100)


class CriticRuleCreate(BaseModel):
    claim_threshold_percent: int = Field(ge=40, le=80)
    ngram_overlap_weight_percent: int = Field(default=0, ge=0, le=50)
    semantic_weight_percent: int = Field(default=0, ge=0, le=60)
    uncertainty_margin_percent: int = Field(default=0, ge=0, le=20)


class MemoryCreate(BaseModel):
    scope: Literal["personal", "project"]
    memory_key: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=300)
    project_id: str | None = None
    expires_on: date | None = None


class MemoryCorrection(BaseModel):
    value: str = Field(min_length=1, max_length=300)
    expires_on: date | None = None


class MemorySuggestionDecision(BaseModel):
    action: Literal["accept", "ignore"]
    expires_on: date | None = None


class BackupRestoreRequest(BaseModel):
    preview_id: str
    confirmation: Literal["RESTORE"]


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings.from_env()
    store = Store(active_settings.database_path)
    runtime = MultiAgentRuntime(store, ArkReportWriter.from_settings(active_settings))
    evolution = EvolutionService(store, runtime)
    app = FastAPI(title="EvoReport", version="0.1.0")
    app.state.settings = active_settings
    app.state.store = store
    app.state.evolution = evolution
    app.state.runtime = runtime

    @app.exception_handler(ModelCallTimeout)
    async def model_timeout_handler(request: Request, exc: ModelCallTimeout):
        return JSONResponse(status_code=504, content={
            "detail": "模型调用超时，运行已中断；请在写作进化页面查看可恢复运行及剩余预算。"
        })

    @app.exception_handler(RoleProcessTimeout)
    async def role_timeout_handler(request: Request, exc: RoleProcessTimeout):
        return JSONResponse(status_code=504, content={
            "detail": "角色等待或执行超时；请在写作进化页面查看失败记录或恢复运行。"
        })

    @app.exception_handler(DailyQueueFull)
    async def daily_queue_full_handler(request: Request, exc: DailyQueueFull):
        return JSONResponse(status_code=503, content={"detail": str(exc) + "；已创建的草稿可在报告历史中查看。"})

    @app.exception_handler(DailyQueueTimeout)
    async def daily_queue_timeout_handler(request: Request, exc: DailyQueueTimeout):
        return JSONResponse(status_code=504, content={"detail": str(exc) + "；已创建的草稿可在报告历史中查看。"})

    def close_role_workers():
        runtime.role_executor.close()

    app.add_event_handler("shutdown", close_role_workers)

    artifact_root = active_settings.artifact_path or active_settings.database_path.parent
    exports_dir = artifact_root / "exports"
    backups_dir = artifact_root / "backups"
    pending_restores_dir = backups_dir / "pending"

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "evoreport", "database": str(active_settings.database_path)}

    @app.get("/api/agents/daily-queue")
    def daily_queue_status():
        return runtime.daily_queue.status()

    @app.get("/api/dashboard")
    def dashboard():
        projects = store.list_projects()
        reports = store.list_reports()
        evolution_status = evolution.status()
        return {
            "projects": len(projects),
            "reports": len(reports),
            "confirmed_reports": sum(1 for item in reports if item["status"] == "confirmed" and item["origin"] != "synthetic"),
            "dataset": evolution_status["dataset"],
            "recent_reports": reports[:5],
            "historical_reports": sum(1 for item in reports if item["origin"] == "historical"),
            "synthetic_reports": sum(1 for item in reports if item["origin"] == "synthetic"),
        }

    @app.get("/api/notes")
    def list_work_notes(note_date: date):
        return store.list_work_notes(note_date.isoformat())

    @app.post("/api/notes", status_code=201)
    def add_work_note(payload: WorkNoteCreate):
        try:
            return store.add_work_note(payload.note_date.isoformat(), payload.content.strip(), payload.project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/reports/parse-work-summary")
    def parse_daily_summary(payload: WorkSummaryParse):
        return parse_work_summary(payload.content)

    @app.get("/api/projects")
    def list_projects():
        return store.list_projects()

    @app.post("/api/projects", status_code=201)
    def add_project(payload: ProjectCreate):
        try:
            path = validate_repository_path(payload.path) if payload.project_type == "git" else validate_directory_path(payload.path)
            return store.add_project(payload.name.strip(), str(path), payload.project_type)
        except (ValueError, GitActivityError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        if not store.delete_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        return {"deleted": True}

    @app.get("/api/projects/{project_id}/activity")
    def project_activity(project_id: str, target_date: date):
        projects = store.get_projects([project_id])
        if not projects:
            raise HTTPException(status_code=404, detail="项目不存在")
        if projects[0]["project_type"] == "plain":
            return {
                "source": "manual", "path": projects[0]["path"], "date": target_date.isoformat(),
                "branch": "", "commits": [], "working_tree": [],
                "summary": {"commit_count": 0, "files_changed": 0, "insertions": 0, "deletions": 0, "uncommitted_count": 0},
            }
        try:
            return collect_activity(projects[0]["path"], target_date)
        except GitActivityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/reports/daily", status_code=201)
    def create_daily(payload: DailyCreate):
        projects = store.get_projects(payload.project_ids)
        if len(projects) != len(set(payload.project_ids)):
            raise HTTPException(status_code=400, detail="包含不存在的项目")
        return _create_daily_report(store, runtime, payload.report_date, projects, payload.completed, payload.problems, payload.tomorrow, [])

    @app.post("/api/reports/daily/quick", status_code=201)
    def create_quick_daily(payload: QuickDailyCreate):
        notes = store.list_work_notes(payload.report_date.isoformat())
        completed = "\n".join(item["content"] for item in reversed(notes))
        return _create_daily_report(
            store, runtime, payload.report_date, store.list_projects(), completed, payload.problems, payload.tomorrow, notes
        )

    @app.post("/api/reports/historical", status_code=201)
    def import_historical_report(payload: HistoricalReportCreate):
        content = payload.content.strip()
        facts = [item.strip() for item in payload.facts if item.strip()] or _extract_historical_facts(content)
        active = store.active_strategy()
        source = {
            "kind": "daily", "origin": "historical", "date": payload.report_date.isoformat(),
            "projects": [], "manual_completed": "\n".join(facts), "manual_problems": "", "manual_tomorrow": "",
            "facts": facts,
        }
        return store.import_historical_report(payload.report_date.isoformat(), source, content, active["version"])

    @app.post("/api/reports/synthetic-samples", status_code=201)
    def import_synthetic_samples():
        active = store.active_strategy()
        created = 0
        skipped = 0
        for report_date, content in SYNTHETIC_DAILY_SAMPLES:
            facts = _extract_historical_facts(content)
            source = {
                "kind": "daily", "origin": "synthetic", "date": report_date, "projects": [],
                "manual_completed": "\n".join(facts), "manual_problems": "", "manual_tomorrow": "", "facts": facts,
            }
            if store.import_synthetic_report(report_date, source, content, active["version"]):
                created += 1
            else:
                skipped += 1
        return {"created": created, "skipped": skipped, "total": len(SYNTHETIC_DAILY_SAMPLES)}

    @app.post("/api/reports/weekly", status_code=201)
    def create_weekly(payload: WeeklyCreate):
        week_end = payload.week_start + timedelta(days=6)
        daily = store.list_reports("daily", payload.week_start.isoformat(), week_end.isoformat())
        confirmed = [item["final_text"] for item in daily if item["status"] == "confirmed" and item["final_text"]]
        if not confirmed:
            raise HTTPException(status_code=400, detail="该周还没有已确认日报")
        active = store.active_strategy()
        strategy = ReportStrategy(version=active["version"], **active["config"])
        label = f"{payload.week_start.isoformat()} 至 {week_end.isoformat()}"
        source = {"kind": "weekly", "date": payload.week_start.isoformat(), "daily_report_ids": [item["id"] for item in daily]}
        draft = generate_weekly(label, confirmed, strategy)
        return store.create_report("weekly", payload.week_start.isoformat(), source, draft, active["version"])

    @app.get("/api/reports")
    def list_reports(kind: str = "", start: date | None = None, end: date | None = None, project_id: str = ""):
        if start and end and start > end:
            raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
        return store.list_reports(kind, start.isoformat() if start else "", end.isoformat() if end else "", project_id)

    @app.get("/api/reports/export/confirmed")
    def export_confirmed_reports():
        reports = [item for item in store.list_reports() if item["status"] == "confirmed" and item["origin"] != "synthetic"]
        exports_dir.mkdir(parents=True, exist_ok=True)
        target = exports_dir / "已确认日报归档.md"
        target.write_text(_render_report_archive(reports), encoding="utf-8")
        return FileResponse(target, media_type="text/markdown; charset=utf-8", filename=target.name)

    @app.get("/api/reports/{report_id}/export")
    def export_report(report_id: str):
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        exports_dir.mkdir(parents=True, exist_ok=True)
        target = exports_dir / f"{report['kind']}-{report['report_date']}-{report['id'][:8]}.md"
        target.write_text(report["final_text"] or report["draft"], encoding="utf-8")
        return FileResponse(target, media_type="text/markdown; charset=utf-8", filename=target.name)

    @app.get("/api/reports/{report_id}/export/docx")
    def export_report_word(report_id: str):
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        target = exports_dir / f"{report['kind']}-{report['report_date']}-{report['id'][:8]}.docx"
        export_report_docx(report, target)
        return FileResponse(
            target,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=target.name,
        )

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str):
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        return report

    @app.get("/api/reports/{report_id}/review-labels")
    def review_labels(report_id: str):
        try:
            return store.get_review_label_case(report_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/reports/{report_id}/review-labels")
    def save_review_labels(report_id: str, payload: ReviewLabelsRequest):
        try:
            return store.save_review_labels(report_id, [item.model_dump() for item in payload.labels],
                                            [item.model_dump() for item in payload.missed_claims])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/review-benchmark")
    def review_benchmark():
        return store.review_benchmark()

    @app.get("/api/agents/critic-rules")
    def critic_rules():
        return store.list_critic_rule_versions()

    @app.post("/api/agents/critic-benchmarks", status_code=201)
    def create_critic_benchmark():
        try:
            return store.create_critic_benchmark()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/critic-rules", status_code=201)
    def create_critic_rule(payload: CriticRuleCreate):
        try:
            return store.create_critic_rule_version(
                payload.claim_threshold_percent, payload.ngram_overlap_weight_percent,
                payload.semantic_weight_percent, payload.uncertainty_margin_percent,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/critic-skills/propose", status_code=201)
    def propose_critic_skill():
        try:
            return store.propose_critic_skill()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/critic-rules/{version}/evaluate")
    def evaluate_critic_rule(version: int):
        try:
            return store.evaluate_critic_rule_version(version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/critic-rules/{version}/gate")
    def gate_critic_rule(version: int):
        try:
            return store.gate_critic_rule_version(version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/critic-rules/{version}/activate")
    def activate_critic_rule(version: int):
        try:
            return store.activate_critic_rule(version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/reports/{report_id}/memory-context")
    def report_memory_context(report_id: str):
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        source = report["source"]
        return {"task_facts": source.get("facts", []), "memory": source.get("memory_context", {})}

    @app.patch("/api/reports/{report_id}/draft")
    def save_report_draft(report_id: str, payload: DraftUpdate):
        try:
            return store.update_report_draft(report_id, payload.draft)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/reports/{report_id}/confirm")
    def confirm_report(report_id: str, payload: ConfirmRequest):
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        if report["status"] != "draft" or report["origin"] != "generated":
            raise HTTPException(status_code=409, detail="只能确认未确认的系统草稿")
        final_text = payload.final_text.strip()
        if not final_text:
            raise HTTPException(status_code=400, detail="终稿不能为空")
        try:
            review_result = runtime.confirm_final(
                report_id, report["source"], final_text, payload.acknowledge_warnings
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if review_result["decision"] != "passed":
            message = "终稿含未脱敏的敏感信息，请修改后再确认" if review_result["decision"] == "blocked" else "终稿审查发现需要核对的内容，请检查后决定是否继续确认"
            raise HTTPException(status_code=409, detail={
                "message": message,
                "review": _review_detail(review_result["review"], report["source"], final_text),
            })
        confirmed = review_result["report"]
        confirmed["final_review"] = {
            "run_id": review_result["run_id"], "warnings_acknowledged": payload.acknowledge_warnings
        }
        if report["kind"] == "daily":
            try:
                confirmed["post_activation_check"] = evolution.maybe_auto_rollback(report_id)
            except Exception:
                confirmed["post_activation_check"] = {"decision": "error", "reason": "终稿已保存，策略监测暂时失败。"}
            try:
                confirmed["writer_prompt_check"] = evolution.maybe_auto_rollback_writer_prompt(report_id)
            except Exception:
                confirmed["writer_prompt_check"] = {
                    "decision": "error", "reason": "终稿已保存，Writer 指令监测暂时失败。",
                }
        return confirmed

    @app.post("/api/backups", status_code=201)
    def create_backup():
        backups_dir.mkdir(parents=True, exist_ok=True)
        target = backups_dir / f"evoreport-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.db"
        store.backup_to(target)
        return {"filename": target.name, "created_at": datetime.now().isoformat(timespec="seconds")}

    @app.post("/api/backups/preview")
    async def preview_backup(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise HTTPException(status_code=415, detail="请选择 SQLite .db 备份文件")
        max_bytes = 64 * 1024 * 1024
        pending_restores_dir.mkdir(parents=True, exist_ok=True)
        preview_id = uuid.uuid4().hex
        staged = pending_restores_dir / f"{preview_id}.db"
        size = 0
        try:
            with staged.open("xb") as target:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(status_code=413, detail="备份文件不能超过 64 MB")
                    target.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="备份文件为空")
            try:
                counts = store.inspect_backup(staged)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"preview_id": preview_id, "size_bytes": size, "counts": counts}
        except Exception:
            staged.unlink(missing_ok=True)
            raise

    @app.post("/api/backups/restore")
    def restore_backup(payload: BackupRestoreRequest):
        try:
            preview_id = uuid.UUID(payload.preview_id).hex
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="无效的备份预览编号") from exc
        staged = pending_restores_dir / f"{preview_id}.db"
        if not staged.is_file():
            raise HTTPException(status_code=404, detail="备份预览已失效，请重新选择文件")
        safety_name = f"evoreport-before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.db"
        safety_path = backups_dir / safety_name
        try:
            counts = store.restore_from(staged, safety_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="恢复失败，当前数据库已尝试从自动备份回滚") from exc
        staged.unlink(missing_ok=True)
        return {"restored": True, "counts": counts, "safety_backup": safety_name}

    @app.get("/api/evolution/status")
    def evolution_status():
        return evolution.status()

    @app.post("/api/evolution/benchmarks", status_code=201)
    def create_evaluation_benchmark():
        try:
            benchmark = evolution.create_benchmark()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"version": benchmark["version"], "fingerprint": benchmark["fingerprint"],
                "validation": len(benchmark["validation_ids"]), "holdout": len(benchmark["holdout_ids"]),
                "coverage": benchmark["coverage"]}

    @app.get("/api/memories")
    def list_memories(active_only: bool = True, include_expired: bool = False):
        return {"memories": store.list_memories(active_only, include_expired)}

    @app.get("/api/memories/suggestions")
    def list_memory_suggestions():
        return {"suggestions": store.list_memory_suggestions()}

    @app.post("/api/memories/suggestions/{suggestion_id}/decision")
    def decide_memory_suggestion(suggestion_id: str, payload: MemorySuggestionDecision):
        try:
            return store.decide_memory_suggestion(
                suggestion_id, payload.action,
                payload.expires_on.isoformat() if payload.expires_on else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/memories", status_code=201)
    def add_memory(payload: MemoryCreate):
        try:
            return store.add_memory(
                payload.scope, payload.memory_key, payload.value, payload.project_id,
                expires_on=payload.expires_on.isoformat() if payload.expires_on else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/memories/{memory_id}")
    def correct_memory(memory_id: str, payload: MemoryCorrection):
        try:
            return store.correct_memory(
                memory_id, payload.value,
                payload.expires_on.isoformat() if payload.expires_on else None,
                update_expiry="expires_on" in payload.model_fields_set,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/memories/{memory_id}")
    def remove_memory(memory_id: str):
        if not store.remove_memory(memory_id):
            raise HTTPException(status_code=404, detail="记忆不存在或已清理")
        return {"removed": True}

    @app.get("/api/agents/overview")
    def agent_overview():
        runs = store.list_agent_runs()
        return {
            "runs": runs,
            "summary": {
                "run_count": len(runs),
                "token_limit": runtime.MAX_TOTAL_TOKENS,
                "passed_runs": sum(1 for item in runs if item["status"] == "passed"),
                "needs_review_runs": sum(1 for item in runs if item["status"] == "needs_review"),
                "average_duration_ms": round(sum(item["duration_ms"] for item in runs) / len(runs)) if runs else 0,
            },
        }

    @app.get("/api/agents/recoverable")
    def recoverable_agent_runs():
        return {"runs": runtime.recoverable_runs() + runtime.recoverable_final_reviews() +
                evolution.recoverable_proposals() + evolution.recoverable_writer_prompt_evaluations()}

    @app.get("/api/agents/writer-prompts")
    def writer_prompts():
        return {"versions": store.list_writer_prompt_versions(), "switches": store.list_writer_prompt_switches(),
                "evaluations": store.list_writer_prompt_evaluations(),
                "post_activation_monitor": evolution.writer_prompt_monitor(),
                "latest_monitor_check": store.latest_writer_prompt_monitor_check(),
                "model_enabled": runtime.writer.enabled}

    @app.post("/api/agents/writer-prompts", status_code=201)
    def create_writer_prompt(payload: WriterPromptCreate):
        try:
            return store.create_writer_prompt(payload.addendum)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/agents/writer-prompts/{version}/activate")
    def activate_writer_prompt(version: int):
        try:
            if not store.activate_writer_prompt(version):
                raise HTTPException(status_code=404, detail="Writer 指令版本不存在")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"activated": True, "version": version}

    @app.post("/api/agents/writer-prompts/{version}/evaluate")
    def evaluate_writer_prompt(version: int):
        try:
            return evolution.evaluate_writer_prompt(version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/writer-prompts/evaluations/{evaluation_id}/resume")
    def resume_writer_prompt_evaluation(evaluation_id: str):
        try:
            return evolution.resume_writer_prompt_evaluation(evaluation_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/agents/{run_id}/replay")
    def replay_agent_run(run_id: str):
        try:
            return runtime.replay_report(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/{run_id}/resume")
    def resume_agent_run(run_id: str, payload: FinalReviewResumeRequest | None = None):
        try:
            run = store.get_agent_run(run_id)
            if run and run["workflow"] == "strategy_evaluation":
                return evolution.resume_proposal(run_id)
            if run and run["workflow"] == "final_review":
                if payload is None:
                    raise ValueError("请提交与原审查相同的终稿")
                review_result = runtime.resume_final_review(run_id, payload.final_text)
                if review_result["decision"] != "passed":
                    message = ("终稿含未脱敏的敏感信息，请修改后再确认"
                               if review_result["decision"] == "blocked" else
                               "终稿审查发现需要核对的内容，请检查后决定是否继续确认")
                    raise HTTPException(status_code=409, detail={
                        "message": message,
                        "review": _review_detail(
                            review_result["review"], store.get_report(run["report_id"])["source"],
                            payload.final_text.strip(),
                        ),
                    })
                confirmed = review_result["report"]
                confirmed["final_review"] = {
                    "run_id": run_id, "warnings_acknowledged": review_result["warnings_acknowledged"],
                }
                if confirmed["kind"] == "daily":
                    try:
                        confirmed["post_activation_check"] = evolution.maybe_auto_rollback(run["report_id"])
                    except Exception:
                        confirmed["post_activation_check"] = {
                            "decision": "error", "reason": "终稿已保存，策略监测暂时失败。",
                        }
                    try:
                        confirmed["writer_prompt_check"] = evolution.maybe_auto_rollback_writer_prompt(
                            run["report_id"]
                        )
                    except Exception:
                        confirmed["writer_prompt_check"] = {
                            "decision": "error", "reason": "终稿已保存，Writer 指令监测暂时失败。",
                        }
                return confirmed
            result = runtime.resume_report(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        run = store.get_agent_run(result["run_id"])
        report = store.get_report(run["report_id"])
        report["agent_trace"] = run
        report["llm_used"] = result["llm_used"]
        return report

    @app.get("/api/llm/status")
    def llm_status():
        writer = runtime.writer
        return {"enabled": writer.enabled, "provider": "火山方舟", "model": writer.model}

    @app.post("/api/evolution/propose")
    def propose_evolution():
        try:
            return evolution.propose()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/evolution/strategies/{version}/activate")
    def activate_strategy(version: int):
        if not store.activate_strategy(version):
            raise HTTPException(status_code=404, detail="策略版本不存在")
        return {"activated": True, "version": version}

    web_root = Path(__file__).resolve().parent.parent / "web"
    app.mount("/assets", StaticFiles(directory=str(web_root)), name="assets")

    @app.get("/")
    def index():
        return FileResponse(web_root / "index.html")

    return app


def _review_detail(review: Dict[str, Any], source: Dict[str, Any], candidate: str) -> Dict[str, Any]:
    facts = source.get("facts", [])
    fact_sources = source.get("fact_sources", [])
    issues = []
    for index in review["missing_fact_indexes"]:
        if index >= len(facts):
            continue
        origin = fact_sources[index] if index < len(fact_sources) else {}
        location = _fact_location(origin, index)
        summary = redact_sensitive(str(facts[index]).replace("\n", " ").strip())[:100]
        issues.append({
            "type": "missing_fact", "line": None, "evidence_id": f"F{index + 1:04d}",
            "summary": f"事实 {index + 1} 未在终稿中找到",
            "evidence": redact_sensitive(f"{location}：{summary}"),
            "match_score": review.get("fact_match_scores", [])[index] if index < len(review.get("fact_match_scores", [])) else 0.0,
            "suggestion": "将该事实补入今日完成，或核对后明确选择继续确认。",
        })
    for heading in review["missing_headings"]:
        issues.append({
            "type": "missing_heading", "line": None, "summary": f"缺少章节：{heading}",
            "evidence": "日报/周报必需章节", "match_score": None,
            "suggestion": f"添加“## {heading}”章节，或核对后明确选择继续确认。",
        })
    lines = candidate.splitlines()
    for claim in review.get("unsupported_claims", []):
        line = claim["line"]
        if 0 < line <= len(lines):
            raw_line = lines[line - 1]
            start, end = claim.get("column_start", 0), claim.get("column_end", len(raw_line))
            snippet = redact_sensitive(raw_line[start:end].strip())[:100]
        else:
            snippet = ""
        matched = claim.get("matched_fact_index")
        evidence = "来源快照中没有完成事项" if matched is None else (
            f"最接近的来源事实 {matched + 1}（{_fact_location(fact_sources[matched] if matched < len(fact_sources) else {}, matched)}）："
            f"{redact_sensitive(str(facts[matched]))[:100]}"
        )
        support_status = claim.get("support_status", "unsupported")
        status_text = "证据不确定" if support_status == "uncertain" else "无匹配证据"
        issues.append({
            "type": "unsupported_claim", "line": line,
            "evidence_id": claim.get("matched_evidence_id"),
            "summary": f"第 {line} 行待核实（{status_text}）的片段：{snippet}",
            "evidence": redact_sensitive(evidence), "match_score": claim["match_score"],
            "support_status": support_status,
            "structure": {key: value for key, value in structured_claim(snippet).items()
                          if key != "semantic_object"},
            "score_components": claim.get("score_components", {}),
            "suggestion": "核对该片段；如属实，请在工作记录中补充来源，或删除/改写该片段。",
        })
    if review["unsafe"]:
        for line in reversed(review.get("sensitive_lines", [None])):
            issues.insert(0, {
                "type": "sensitive", "line": line,
                "summary": f"第 {line} 行检测到未脱敏的敏感内容" if line else "检测到未脱敏的敏感内容",
                "evidence": "为避免泄露，不展示匹配内容", "match_score": None,
                "suggestion": "删除或脱敏敏感内容后重新审查。",
            })
    return {
        "unsafe": review["unsafe"],
        "missing_headings": review["missing_headings"],
        "missing_facts": [
            {"number": index + 1, "summary": redact_sensitive(str(facts[index]).replace("\n", " ").strip())[:100]}
            for index in review["missing_fact_indexes"] if index < len(facts)
        ],
        "issues": issues,
    }


def _fact_location(origin: Dict[str, Any], index: int) -> str:
    if origin.get("source_type") == "git_commit":
        return f"{origin.get('project_name', '项目')} · 提交 {str(origin.get('commit_hash', ''))[:12]}"
    if origin.get("source_type") == "manual":
        return f"手工记录第 {origin.get('line')} 行"
    return f"来源快照事实 {index + 1}"


def _manual_activity(project: dict, target_date: date) -> dict:
    return {
        "source": "manual", "path": project["path"], "date": target_date.isoformat(),
        "branch": "", "commits": [], "working_tree": [],
        "summary": {"commit_count": 0, "files_changed": 0, "insertions": 0, "deletions": 0, "uncommitted_count": 0},
    }


def _extract_historical_facts(content: str) -> List[str]:
    facts = []
    for line in content.splitlines():
        item = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
        item = re.sub(r"^#+\s*", "", item)
        if item and not item.startswith("**") and len(item) >= 6:
            facts.append(item)
    return facts[:100]


def _render_report_archive(reports: List[dict]) -> str:
    if not reports:
        return "# EvoReport 已确认报告归档\n\n暂无已确认的真实报告。\n"
    sections = ["# EvoReport 已确认报告归档"]
    for report in reports:
        label = "周报" if report["kind"] == "weekly" else "日报"
        sections.append(f"---\n\n## {label} · {report['report_date']}\n\n{report['final_text'] or report['draft']}")
    return "\n\n".join(sections).rstrip() + "\n"


def _create_daily_report(
    store: Store, runtime: MultiAgentRuntime, report_date: date, projects: List[dict], completed: str,
    problems: str, tomorrow: str, notes: List[dict]
):
    enriched = []
    try:
        for project in projects:
            activity = collect_activity(project["path"], report_date) if project["project_type"] == "git" else _manual_activity(project, report_date)
            enriched.append({**project, "activity": activity})
    except GitActivityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    active = store.active_strategy()
    memory = store.memory_context([item["id"] for item in projects])
    project_preferences = memory.get("project_guidance", {}).get("expression_preferences", {})
    effective_config = {**active["config"], **project_preferences, **memory["preferences"]}
    strategy = ReportStrategy(version=active["version"], **effective_config)
    source = {
        "kind": "daily", "date": report_date.isoformat(), "projects": enriched,
        "manual_completed": completed, "manual_problems": problems, "manual_tomorrow": tomorrow,
        "quick_notes": notes,
    }
    fact_records = source_fact_records(enriched, completed)
    source["facts"] = [item["fact"] for item in fact_records]
    source["fact_sources"] = [{key: value for key, value in item.items() if key != "fact"} for item in fact_records]
    source["strategy_version"] = active["version"]
    source["effective_strategy"] = effective_config
    source["memory_context"] = memory
    draft = generate_daily(report_date, enriched, completed, problems, tomorrow, strategy)
    report = store.create_report("daily", report_date.isoformat(), source, draft, active["version"])
    agent_result = runtime.run_report(report["id"], source, draft)
    report = store.get_report(report["id"])
    report["agent_trace"] = store.get_agent_run(agent_result["run_id"])
    report["llm_used"] = agent_result["llm_used"]
    return report


app = create_app()


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
