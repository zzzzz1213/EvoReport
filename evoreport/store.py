from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .evaluation import (CRITIC_MATCH_ALGORITHM_VERSION, CRITIC_MATCH_THRESHOLD_PERCENT,
                         review_claims, review_completion_lines, review_report, summarize_edit_feedback)
from .agent_instructions import CRITIC_RULE_VERSION, instruction_hash, validate_writer_addendum, writer_instruction
from .memory_suggestions import (memories_conflict, memory_similarity, normalized_memory_text,
                                 suggest_personal_memories, suggest_project_memories)
from .report_generator import redact_sensitive

REVIEW_SCENARIOS = ("compound", "paraphrase", "implicit")
CRITIC_REQUIRED_CHECKS = (
    "missing_facts", "required_headings", "sensitive_information", "unsupported_claims",
)


def _review_scenarios(value: Any) -> List[str]:
    if (not isinstance(value, list) or any(not isinstance(item, str) or item not in REVIEW_SCENARIOS for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("审查场景标签无效")
    return [item for item in REVIEW_SCENARIOS if item in value]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memory_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def _source_sha256(source_json: str) -> str:
    source = json.loads(source_json)
    payload = json.dumps(source, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _critic_skill(threshold_percent: int, ngram_overlap_weight_percent: int = 0,
                  semantic_weight_percent: int = 0, uncertainty_margin_percent: int = 0,
                  schema_version: int = 1) -> Dict[str, Any]:
    skill = {
        "schema_version": schema_version,
        "claim_match": {
            "threshold_percent": threshold_percent,
            "ngram_overlap_weight_percent": ngram_overlap_weight_percent,
        },
        "required_checks": list(CRITIC_REQUIRED_CHECKS),
    }
    if schema_version == 2:
        skill["claim_match"].update({
            "semantic_weight_percent": semantic_weight_percent,
            "uncertainty_margin_percent": uncertainty_margin_percent,
        })
    return _validate_critic_skill(skill, allow_legacy=True)


def _validate_critic_skill(value: Any, allow_legacy: bool = False) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "claim_match", "required_checks"}:
        raise ValueError("Critic Skill 必须是受支持的声明式 JSON")
    schema_version = value["schema_version"]
    allowed_versions = {0, 1, 2} if allow_legacy else {1, 2}
    if type(schema_version) is not int or schema_version not in allowed_versions:
        raise ValueError("Critic Skill schema_version 无效")
    claim_match = value["claim_match"]
    expected_fields = {"threshold_percent", "ngram_overlap_weight_percent"}
    if schema_version == 2:
        expected_fields |= {"semantic_weight_percent", "uncertainty_margin_percent"}
    if not isinstance(claim_match, dict) or set(claim_match) != expected_fields:
        raise ValueError("Critic Skill 匹配规则字段无效")
    threshold = claim_match["threshold_percent"]
    weight = claim_match["ngram_overlap_weight_percent"]
    if type(threshold) is not int or not 40 <= threshold <= 80:
        raise ValueError("匹配阈值须为 40% 至 80% 的整数")
    if type(weight) is not int or not 0 <= weight <= 50:
        raise ValueError("字符重合度权重须为 0% 至 50% 的整数")
    semantic_weight = claim_match.get("semantic_weight_percent", 0)
    uncertainty_margin = claim_match.get("uncertainty_margin_percent", 0)
    if type(semantic_weight) is not int or not 0 <= semantic_weight <= 60:
        raise ValueError("结构化语义权重须为 0% 至 60% 的整数")
    if type(uncertainty_margin) is not int or not 0 <= uncertainty_margin <= 20:
        raise ValueError("待核实区间须为 0% 至 20% 的整数")
    checks = value["required_checks"]
    if checks != list(CRITIC_REQUIRED_CHECKS):
        raise ValueError("Critic Skill 不能关闭或替换强制检查")
    result = {
        "schema_version": schema_version,
        "claim_match": {"threshold_percent": threshold, "ngram_overlap_weight_percent": weight},
        "required_checks": list(CRITIC_REQUIRED_CHECKS),
    }
    if schema_version == 2:
        result["claim_match"].update({"semantic_weight_percent": semantic_weight,
                                      "uncertainty_margin_percent": uncertainty_margin})
    return result


def _critic_rule_fingerprint(skill: Dict[str, Any] | int) -> str:
    if isinstance(skill, int):
        skill = _critic_skill(skill, schema_version=0)
    skill = _validate_critic_skill(skill, allow_legacy=True)
    if skill["schema_version"] == 0:
        payload = {"algorithm_version": CRITIC_MATCH_ALGORITHM_VERSION,
                   "claim_threshold_percent": skill["claim_match"]["threshold_percent"]}
    else:
        payload = {"algorithm_version": CRITIC_MATCH_ALGORITHM_VERSION, "skill": skill}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _critic_rule(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    raw = item.get("skill_json")
    try:
        skill = json.loads(raw) if raw else _critic_skill(item["claim_threshold_percent"], schema_version=0)
        proposal = json.loads(item.get("proposal_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Critic Skill JSON 已损坏") from exc
    if not isinstance(proposal, dict):
        raise ValueError("Critic Skill 提案证据无效")
    item["skill"] = _validate_critic_skill(skill, allow_legacy=True)
    item["proposal"] = proposal
    item.pop("skill_json", None)
    item.pop("proposal_json", None)
    return item


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _init_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    project_type TEXT NOT NULL DEFAULT 'git',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'generated',
                    source_json TEXT NOT NULL,
                    draft TEXT NOT NULL,
                    initial_draft TEXT,
                    final_text TEXT,
                    status TEXT NOT NULL,
                    strategy_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_notes (
                    id TEXT PRIMARY KEY,
                    note_date TEXT NOT NULL,
                    project_id TEXT,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL UNIQUE,
                    split TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    expected_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(report_id) REFERENCES reports(id)
                );
                CREATE TABLE IF NOT EXISTS review_claim_labels (
                    report_id TEXT NOT NULL,
                    claim_index INTEGER NOT NULL,
                    label TEXT NOT NULL CHECK(label IN ('supported','unsupported')),
                    source_sha256 TEXT NOT NULL,
                    final_sha256 TEXT NOT NULL,
                    claims_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(report_id,claim_index),
                    FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS review_missed_claims (
                    report_id TEXT NOT NULL,
                    claim_index INTEGER NOT NULL,
                    line INTEGER NOT NULL,
                    column_start INTEGER NOT NULL,
                    column_end INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    final_sha256 TEXT NOT NULL,
                    claims_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(report_id,claim_index),
                    FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS critic_rule_versions (
                    version INTEGER PRIMARY KEY,
                    claim_threshold_percent INTEGER NOT NULL,
                    algorithm_version INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    skill_json TEXT NOT NULL DEFAULT '{}',
                    proposal_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_threshold_percent,algorithm_version)
                );
                CREATE TABLE IF NOT EXISTS critic_rule_evaluations (
                    id TEXT PRIMARY KEY,
                    candidate_version INTEGER NOT NULL,
                    corpus_fingerprint TEXT NOT NULL,
                    baseline_json TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(candidate_version,corpus_fingerprint),
                    FOREIGN KEY(candidate_version) REFERENCES critic_rule_versions(version)
                );
                CREATE TABLE IF NOT EXISTS critic_benchmarks (
                    version INTEGER PRIMARY KEY,
                    validation_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    holdout_used_at TEXT,
                    holdout_result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS critic_benchmark_members (
                    version INTEGER NOT NULL,
                    report_id TEXT NOT NULL,
                    split TEXT NOT NULL CHECK(split IN ('validation','holdout','excluded')),
                    PRIMARY KEY(version,report_id),
                    FOREIGN KEY(version) REFERENCES critic_benchmarks(version),
                    FOREIGN KEY(report_id) REFERENCES reports(id)
                );
                CREATE TABLE IF NOT EXISTS critic_rule_switches (
                    id TEXT PRIMARY KEY,
                    previous_version INTEGER NOT NULL,
                    active_version INTEGER NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('activation','rollback')),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(previous_version) REFERENCES critic_rule_versions(version),
                    FOREIGN KEY(active_version) REFERENCES critic_rule_versions(version)
                );
                CREATE TABLE IF NOT EXISTS writing_feedback (
                    report_id TEXT PRIMARY KEY,
                    split TEXT NOT NULL,
                    signals_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS strategies (
                    version INTEGER PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_activations (
                    id TEXT PRIMARY KEY,
                    previous_version INTEGER NOT NULL,
                    active_version INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(previous_version) REFERENCES strategies(version),
                    FOREIGN KEY(active_version) REFERENCES strategies(version)
                );
                CREATE TABLE IF NOT EXISTS strategy_monitor_checks (
                    id TEXT PRIMARY KEY,
                    activation_id TEXT NOT NULL,
                    last_case_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(activation_id,last_case_id),
                    FOREIGN KEY(activation_id) REFERENCES strategy_activations(id)
                );
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY,
                    candidate_version INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_benchmarks (
                    version INTEGER PRIMARY KEY,
                    validation_ids_json TEXT NOT NULL,
                    holdout_ids_json TEXT NOT NULL,
                    validation_hash TEXT NOT NULL,
                    holdout_hash TEXT NOT NULL,
                    validation_draft_hash TEXT,
                    holdout_draft_hash TEXT,
                    coverage_json TEXT,
                    fingerprint TEXT NOT NULL,
                    holdout_used_at TEXT,
                    holdout_run_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK(scope IN ('personal','project')),
                    project_id TEXT,
                    memory_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    supersedes_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_on TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_suggestion_decisions (
                    suggestion_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('accepted','ignored')),
                    memory_id TEXT,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS personal_memory_suggestion_decisions (
                    suggestion_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('accepted','ignored')),
                    memory_id TEXT,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS writer_prompt_versions (
                    version INTEGER PRIMARY KEY,
                    addendum TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS writer_prompt_switches (
                    id TEXT PRIMARY KEY,
                    previous_version INTEGER NOT NULL,
                    active_version INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(previous_version) REFERENCES writer_prompt_versions(version),
                    FOREIGN KEY(active_version) REFERENCES writer_prompt_versions(version)
                );
                CREATE TABLE IF NOT EXISTS writer_prompt_monitor_checks (
                    id TEXT PRIMARY KEY,
                    switch_id TEXT NOT NULL,
                    last_case_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(switch_id,last_case_id),
                    FOREIGN KEY(switch_id) REFERENCES writer_prompt_switches(id)
                );
                CREATE TABLE IF NOT EXISTS writer_prompt_evaluations (
                    id TEXT PRIMARY KEY,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER NOT NULL,
                    benchmark_version INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'legacy',
                    progress_json TEXT,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(candidate_version,benchmark_version),
                    FOREIGN KEY(candidate_version) REFERENCES writer_prompt_versions(version),
                    FOREIGN KEY(baseline_version) REFERENCES writer_prompt_versions(version),
                    FOREIGN KEY(benchmark_version) REFERENCES evaluation_benchmarks(version)
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    report_id TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    token_used INTEGER NOT NULL DEFAULT 0,
                    token_estimated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    completed_at TEXT,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    instruction_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(report_id) REFERENCES reports(id)
                );
                CREATE TABLE IF NOT EXISTS agent_steps (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    run_id TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    candidate_draft TEXT NOT NULL,
                    review_json TEXT NOT NULL,
                    handoff_json TEXT NOT NULL DEFAULT '{}',
                    input_hash TEXT NOT NULL,
                    llm_used INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS agent_step_snapshots (
                    step_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    candidate_draft TEXT NOT NULL,
                    review_json TEXT NOT NULL,
                    handoff_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    replay_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(step_id) REFERENCES agent_steps(id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS strategy_evaluation_checkpoints (
                    run_id TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
                );
                """
            )
            existing = connection.execute("SELECT version FROM strategies LIMIT 1").fetchone()
            connection.execute(
                """INSERT OR IGNORE INTO critic_rule_versions(
                    version,claim_threshold_percent,algorithm_version,fingerprint,source,created_at
                ) VALUES(1,?,?,?,?,?)""",
                (CRITIC_MATCH_THRESHOLD_PERCENT, CRITIC_MATCH_ALGORITHM_VERSION,
                 _critic_rule_fingerprint(CRITIC_MATCH_THRESHOLD_PERCENT), "builtin", utc_now()),
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()}
            for table in ("review_claim_labels", "review_missed_claims"):
                review_columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "claims_sha256" not in review_columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN claims_sha256 TEXT NOT NULL DEFAULT ''"
                    )
                if "scenarios_json" not in review_columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN scenarios_json TEXT NOT NULL DEFAULT '[]'")
            if "project_type" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN project_type TEXT NOT NULL DEFAULT 'git'")
            report_columns = {row["name"] for row in connection.execute("PRAGMA table_info(reports)").fetchall()}
            if "origin" not in report_columns:
                connection.execute("ALTER TABLE reports ADD COLUMN origin TEXT NOT NULL DEFAULT 'generated'")
            if "initial_draft" not in report_columns:
                connection.execute("ALTER TABLE reports ADD COLUMN initial_draft TEXT")
                connection.execute("UPDATE reports SET initial_draft=draft WHERE initial_draft IS NULL")
            run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if "updated_at" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN updated_at TEXT")
                connection.execute("UPDATE agent_runs SET updated_at=COALESCE(completed_at,created_at)")
            if "recovery_count" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0")
            if "error_code" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN error_code TEXT")
            if "instruction_json" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN instruction_json TEXT NOT NULL DEFAULT '{}'")
            if "token_used" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN token_used INTEGER NOT NULL DEFAULT 0")
            if "token_estimated" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN token_estimated INTEGER NOT NULL DEFAULT 0")
            checkpoint_columns = {row["name"] for row in connection.execute("PRAGMA table_info(agent_checkpoints)").fetchall()}
            if "handoff_json" not in checkpoint_columns:
                connection.execute("ALTER TABLE agent_checkpoints ADD COLUMN handoff_json TEXT NOT NULL DEFAULT '{}'")
            evaluation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(evaluation_runs)").fetchall()}
            if "evidence_json" not in evaluation_columns:
                connection.execute("ALTER TABLE evaluation_runs ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'")
            benchmark_columns = {row["name"] for row in connection.execute("PRAGMA table_info(evaluation_benchmarks)").fetchall()}
            if "validation_draft_hash" not in benchmark_columns:
                connection.execute("ALTER TABLE evaluation_benchmarks ADD COLUMN validation_draft_hash TEXT")
            if "holdout_draft_hash" not in benchmark_columns:
                connection.execute("ALTER TABLE evaluation_benchmarks ADD COLUMN holdout_draft_hash TEXT")
            if "coverage_json" not in benchmark_columns:
                connection.execute("ALTER TABLE evaluation_benchmarks ADD COLUMN coverage_json TEXT")
            if "scenarios_json" not in benchmark_columns:
                connection.execute("ALTER TABLE evaluation_benchmarks ADD COLUMN scenarios_json TEXT")
            if "holdout_run_id" not in benchmark_columns:
                connection.execute("ALTER TABLE evaluation_benchmarks ADD COLUMN holdout_run_id TEXT")
            memory_columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_entries)")}
            if "expires_on" not in memory_columns:
                connection.execute("ALTER TABLE memory_entries ADD COLUMN expires_on TEXT")
            prompt_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(writer_prompt_evaluations)").fetchall()}
            prompt_additions = {
                "stage": "TEXT NOT NULL DEFAULT 'legacy'", "progress_json": "TEXT",
                "model_calls": "INTEGER NOT NULL DEFAULT 0", "duration_ms": "INTEGER NOT NULL DEFAULT 0",
                "recovery_count": "INTEGER NOT NULL DEFAULT 0", "updated_at": "TEXT",
            }
            for column, definition in prompt_additions.items():
                if column not in prompt_columns:
                    connection.execute(f"ALTER TABLE writer_prompt_evaluations ADD COLUMN {column} {definition}")
            prompt_switch_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(writer_prompt_switches)").fetchall()}
            if "source" not in prompt_switch_columns:
                connection.execute(
                    "ALTER TABLE writer_prompt_switches ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
                )
            critic_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(critic_rule_versions)").fetchall()}
            if "skill_json" not in critic_columns:
                connection.execute("ALTER TABLE critic_rule_versions ADD COLUMN skill_json TEXT NOT NULL DEFAULT '{}'")
            if "proposal_json" not in critic_columns:
                connection.execute("ALTER TABLE critic_rule_versions ADD COLUMN proposal_json TEXT NOT NULL DEFAULT '{}'")
            for row in connection.execute(
                "SELECT version,claim_threshold_percent,skill_json FROM critic_rule_versions"
            ).fetchall():
                if not row["skill_json"] or row["skill_json"] == "{}":
                    connection.execute(
                        "UPDATE critic_rule_versions SET skill_json=? WHERE version=?",
                        (json.dumps(_critic_skill(row["claim_threshold_percent"], schema_version=0),
                                    ensure_ascii=False, sort_keys=True), row["version"]),
                    )
            if existing is None:
                connection.execute(
                    "INSERT INTO strategies(version,config_json,metrics_json,status,created_at) VALUES(?,?,?,?,?)",
                    (1, json.dumps({"numbered": True, "include_statistics": True, "max_item_length": 100}), "{}", "active", utc_now()),
                )
            if connection.execute("SELECT 1 FROM writer_prompt_versions LIMIT 1").fetchone() is None:
                instruction = writer_instruction("")
                connection.execute(
                    """INSERT INTO writer_prompt_versions(version,addendum,instruction,sha256,source,status,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (1, "", instruction, instruction_hash(instruction), "builtin", "active", utc_now()),
                )

    def add_project(self, name: str, path: str, project_type: str) -> Dict[str, Any]:
        project_id = str(uuid.uuid4())
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO projects(id,name,path,project_type,created_at) VALUES(?,?,?,?,?)",
                    (project_id, name, path, project_type, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该项目目录已添加") from exc
        return {"id": project_id, "name": name, "path": path, "project_type": project_type, "created_at": created_at}

    def list_projects(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_projects(self, project_ids: Sequence[str]) -> List[Dict[str, Any]]:
        if not project_ids:
            return []
        placeholders = ",".join("?" for _ in project_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM projects WHERE id IN ({placeholders}) ORDER BY name", tuple(project_ids)
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_project(self, project_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return cursor.rowcount > 0

    def add_work_note(self, note_date: str, content: str, project_id: Optional[str] = None) -> Dict[str, Any]:
        note_id = str(uuid.uuid4())
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            if project_id:
                project = connection.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
                if project is None:
                    raise ValueError("关联项目不存在")
            connection.execute(
                "INSERT INTO work_notes(id,note_date,project_id,content,created_at) VALUES(?,?,?,?,?)",
                (note_id, note_date, project_id or None, content, created_at),
            )
        return self.get_work_note(note_id)

    def get_work_note(self, note_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT notes.*, projects.name AS project_name FROM work_notes AS notes
                LEFT JOIN projects ON projects.id=notes.project_id WHERE notes.id=?""",
                (note_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_work_notes(self, note_date: str) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT notes.*, projects.name AS project_name FROM work_notes AS notes
                LEFT JOIN projects ON projects.id=notes.project_id WHERE notes.note_date=?
                ORDER BY notes.created_at DESC""",
                (note_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_report(
        self, kind: str, report_date: str, source: Dict[str, Any], draft: str, strategy_version: int, origin: str = "generated"
    ) -> Dict[str, Any]:
        report_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO reports(
                    id,kind,report_date,origin,source_json,draft,initial_draft,final_text,status,strategy_version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (report_id, kind, report_date, origin, json.dumps(source, ensure_ascii=False), draft, draft, None, "draft", strategy_version, now, now),
            )
        return self.get_report(report_id)

    def import_historical_report(
        self, report_date: str, source: Dict[str, Any], content: str, strategy_version: int
    ) -> Dict[str, Any]:
        report_id = str(uuid.uuid4())
        case_id = str(uuid.uuid4())
        now = utc_now()
        split = "holdout" if int(case_id.replace("-", "")[:2], 16) % 5 == 0 else "validation"
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO reports(
                    id,kind,report_date,origin,source_json,draft,initial_draft,final_text,status,strategy_version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (report_id, "daily", report_date, "historical", json.dumps(source, ensure_ascii=False), content, content, content, "confirmed", strategy_version, now, now),
            )
            connection.execute(
                """INSERT INTO evaluation_cases(
                    id,report_id,split,source_json,expected_text,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (case_id, report_id, split, json.dumps(source, ensure_ascii=False), content, now),
            )
        return self.get_report(report_id)

    def import_synthetic_report(
        self, report_date: str, source: Dict[str, Any], content: str, strategy_version: int
    ) -> Optional[Dict[str, Any]]:
        """保存仅用于体验的模拟日报；它不进入个人评测数据集。"""
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT id FROM reports WHERE origin='synthetic' AND report_date=?", (report_date,)
            ).fetchone()
            if existing:
                return None
            report_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                """INSERT INTO reports(
                    id,kind,report_date,origin,source_json,draft,initial_draft,final_text,status,strategy_version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (report_id, "daily", report_date, "synthetic", json.dumps(source, ensure_ascii=False), content, content, content, "confirmed", strategy_version, now, now),
            )
        return self.get_report(report_id)

    def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return self._report(row) if row else None

    @staticmethod
    def _review_label_snapshot(connection: sqlite3.Connection, report_id: str) -> Dict[str, Any]:
        row = connection.execute(
            """SELECT r.source_json,r.final_text FROM reports AS r
            JOIN evaluation_cases AS c ON c.report_id=r.id
            WHERE r.id=? AND r.kind='daily' AND r.origin='generated'
            AND r.status='confirmed' AND c.split='validation'""", (report_id,),
        ).fetchone()
        if row is None or not row["final_text"]:
            raise ValueError("仅可标注已确认的 Validation 系统日报")
        source = json.loads(row["source_json"])
        claims = review_claims(row["final_text"])
        active_rule = _critic_rule(Store._active_critic_rule_in_connection(connection))
        match = active_rule["skill"]["claim_match"]
        issues = review_report(source, row["final_text"], match["threshold_percent"],
                               match["ngram_overlap_weight_percent"],
                               match.get("semantic_weight_percent", 0),
                               match.get("uncertainty_margin_percent", 0))["unsupported_claims"]
        flagged = {(item["line"], item["column_start"], item["column_end"]) for item in issues}
        statuses = {(item["line"], item["column_start"], item["column_end"]):
                    item.get("support_status", "unsupported") for item in issues}
        for claim in claims:
            location = (claim["line"], claim["column_start"], claim["column_end"])
            claim["flagged"] = location in flagged
            claim["support_status"] = statuses.get(location, "supported")
        claim_locations = [{key: claim[key] for key in ("line", "column_start", "column_end", "text")}
                           for claim in claims]
        return {"claims": claims, "completion_lines": review_completion_lines(row["final_text"]),
                "raw_lines": row["final_text"].splitlines(),
                "source_sha256": _source_sha256(row["source_json"]),
                "final_sha256": hashlib.sha256(row["final_text"].encode("utf-8")).hexdigest(),
                "claims_sha256": hashlib.sha256(json.dumps(
                    claim_locations, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()}

    @staticmethod
    def _review_missed_span(snapshot: Dict[str, Any], line: int, start: int, end: int,
                            previous: List[Dict[str, Any]]) -> Dict[str, Any]:
        if line not in {item["line"] for item in snapshot["completion_lines"]}:
            raise ValueError("补标片段必须位于今日完成章节")
        raw = snapshot["raw_lines"][line - 1]
        if start < 0 or end > len(raw) or end <= start:
            raise ValueError("补标片段位置无效")
        fragment = raw[start:end]
        prefix = re.match(r"^\s*(?:[-*]|\d+[.)、])\s+", raw)
        if fragment != fragment.strip() or not re.search(r"\w", fragment) or (prefix and start < prefix.end()):
            raise ValueError("请选择今日完成中的完整事项文字")
        overlapping = [claim for claim in snapshot["claims"] if claim["line"] == line
                       and start < claim["column_end"] and claim["column_start"] < end]
        parent = None
        if overlapping:
            if len(overlapping) != 1 or not (
                overlapping[0]["column_start"] <= start < end <= overlapping[0]["column_end"]
            ) or (start == overlapping[0]["column_start"] and end == overlapping[0]["column_end"]):
                raise ValueError("补标子句须完整位于一个已识别事项内")
            parent = overlapping[0]["index"]
        for claim in previous:
            if claim["line"] == line and start < claim["column_end"] and claim["column_start"] < end:
                raise ValueError("补标片段与已有补标重叠，请调整选择")
        return {"index": len(previous), "line": line, "column_start": start,
                "column_end": end, "text": redact_sensitive(fragment), "flagged": False,
                "label": "unsupported", "parent_claim_index": parent}

    def get_review_label_case(self, report_id: str) -> Dict[str, Any]:
        with self._connection() as connection:
            snapshot = self._review_label_snapshot(connection, report_id)
            rows = connection.execute(
                "SELECT * FROM review_claim_labels WHERE report_id=? ORDER BY claim_index", (report_id,)
            ).fetchall()
            missed_rows = connection.execute(
                "SELECT * FROM review_missed_claims WHERE report_id=? ORDER BY claim_index", (report_id,)
            ).fetchall()
        valid = bool(rows or missed_rows) and len(rows) == len(snapshot["claims"]) and all(
            row["claim_index"] == index and row["source_sha256"] == snapshot["source_sha256"]
            and row["final_sha256"] == snapshot["final_sha256"]
            and row["claims_sha256"] == snapshot["claims_sha256"]
            for index, row in enumerate(rows)
        )
        missed_claims = []
        if valid:
            try:
                for index, row in enumerate(missed_rows):
                    if row["claim_index"] != index or any(
                        row[key] != snapshot[key] for key in ("source_sha256", "final_sha256", "claims_sha256")
                    ):
                        raise ValueError("补标快照已变化")
                    missed_claims.append(self._review_missed_span(
                        snapshot, row["line"], row["column_start"], row["column_end"], missed_claims
                    ))
                    missed_claims[-1]["scenarios"] = _review_scenarios(json.loads(row["scenarios_json"]))
                if any(claim["parent_claim_index"] is not None
                       and rows[claim["parent_claim_index"]]["label"] != "unsupported"
                       for claim in missed_claims):
                    raise ValueError("补标子句的父事项须标为无来源")
            except (ValueError, TypeError, json.JSONDecodeError):
                valid = False
                missed_claims = []
        if valid:
            try:
                label_scenarios = [_review_scenarios(json.loads(row["scenarios_json"])) for row in rows]
            except (ValueError, TypeError, json.JSONDecodeError):
                valid = False
                missed_claims = []
        for claim in snapshot["claims"]:
            claim["label"] = rows[claim["index"]]["label"] if valid else None
            claim["scenarios"] = label_scenarios[claim["index"]] if valid else []
        return {"report_id": report_id, "claims": snapshot["claims"],
                "completion_lines": snapshot["completion_lines"],
                "missed_claims": missed_claims, "labeled": valid,
                "stale": bool(rows or missed_rows) and not valid}

    def save_review_labels(self, report_id: str, labels: List[Dict[str, Any]],
                           missed_claims: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        missed_claims = missed_claims or []
        with self._lock, self._connection() as connection:
            snapshot = self._review_label_snapshot(connection, report_id)
            if not (snapshot["claims"] or missed_claims) or [item.get("index") for item in labels] != list(range(len(snapshot["claims"]))):
                raise ValueError("请逐项标注当前终稿的全部完成事项")
            if any(item.get("label") not in {"supported", "unsupported"} for item in labels):
                raise ValueError("事项标签无效")
            label_scenarios = [_review_scenarios(item.get("scenarios", [])) for item in labels]
            spans = []
            for item in missed_claims:
                spans.append(self._review_missed_span(
                    snapshot, item["line"], item["column_start"], item["column_end"], spans
                ))
                spans[-1]["scenarios"] = _review_scenarios(item.get("scenarios", []))
            if any(item["parent_claim_index"] is not None
                   and labels[item["parent_claim_index"]]["label"] != "unsupported" for item in spans):
                raise ValueError("补标子句的父事项须标为无来源")
            now = utc_now()
            connection.execute("DELETE FROM review_claim_labels WHERE report_id=?", (report_id,))
            connection.execute("DELETE FROM review_missed_claims WHERE report_id=?", (report_id,))
            connection.executemany(
                """INSERT INTO review_claim_labels(
                    report_id,claim_index,label,source_sha256,final_sha256,claims_sha256,created_at,updated_at,scenarios_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                [(report_id, item["index"], item["label"], snapshot["source_sha256"],
                  snapshot["final_sha256"], snapshot["claims_sha256"], now, now,
                  json.dumps(label_scenarios[item["index"]])) for item in labels],
            )
            connection.executemany(
                """INSERT INTO review_missed_claims(
                    report_id,claim_index,line,column_start,column_end,
                    source_sha256,final_sha256,claims_sha256,created_at,updated_at,scenarios_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(report_id, item["index"], item["line"], item["column_start"], item["column_end"],
                  snapshot["source_sha256"], snapshot["final_sha256"], snapshot["claims_sha256"], now, now,
                  json.dumps(item["scenarios"]))
                 for item in spans],
            )
            active = connection.execute(
                "SELECT version,holdout_used_at FROM critic_benchmarks ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if active and active["holdout_used_at"] is None:
                connection.execute(
                    """INSERT OR IGNORE INTO critic_benchmark_members(version,report_id,split)
                    VALUES(?,?,'holdout')""", (active["version"], report_id),
                )
        return self.get_review_label_case(report_id)

    def _review_benchmark_snapshot(self, skill: Dict[str, Any] | int,
                                   report_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        skill = (_critic_skill(skill, schema_version=0) if isinstance(skill, int)
                 else _validate_critic_skill(skill, allow_legacy=True))
        match = skill["claim_match"]
        with self._lock:
            if report_ids is None:
                with self._connection() as connection:
                    report_ids = [row["report_id"] for row in connection.execute(
                        "SELECT report_id FROM review_claim_labels UNION SELECT report_id FROM review_missed_claims ORDER BY report_id"
                    )]
            counts = {"true_positive": 0, "false_positive": 0, "false_negative": 0, "true_negative": 0}
            decision_counts = {"supported": 0, "uncertain": 0, "unsupported": 0}
            mismatches = []
            events = []
            corpus = []
            stale_reports = 0
            nested_claims = 0
            project_ids = set()
            scenario_counts = {name: {"items": 0, "false_positive": 0, "false_negative": 0}
                               for name in REVIEW_SCENARIOS}
            untagged = 0
            for report_id in report_ids:
                try:
                    case = self.get_review_label_case(report_id)
                except ValueError:
                    stale_reports += 1
                    continue
                if not case["labeled"]:
                    stale_reports += 1
                    continue
                report = self.get_report(report_id)
                project_ids.update(project["id"] for project in report["source"].get("projects", [])
                                   if isinstance(project, dict) and isinstance(project.get("id"), str))
                corpus.append({"report_id": report_id, "source_sha256": _source_sha256(json.dumps(
                    report["source"], ensure_ascii=False)),
                    "final_sha256": hashlib.sha256(report["final_text"].encode("utf-8")).hexdigest(),
                    "labels": [(claim["index"], claim["label"], claim["line"],
                                claim["column_start"], claim["column_end"]) for claim in case["claims"]],
                    "missed": [(claim["line"], claim["column_start"], claim["column_end"])
                               for claim in case["missed_claims"]]})
                scenario_labels = [(claim["index"], claim["scenarios"]) for claim in case["claims"]
                                   if claim["scenarios"]]
                scenario_missed = [(claim["index"], claim["scenarios"]) for claim in case["missed_claims"]
                                   if claim["scenarios"]]
                if scenario_labels or scenario_missed:
                    corpus[-1]["scenarios"] = {"labels": scenario_labels, "missed": scenario_missed}
                issues = review_report(
                    report["source"], report["final_text"], match["threshold_percent"],
                    match["ngram_overlap_weight_percent"],
                    match.get("semantic_weight_percent", 0),
                    match.get("uncertainty_margin_percent", 0),
                )["unsupported_claims"]
                locations = {(item["line"], item["column_start"], item["column_end"]) for item in issues}
                issue_statuses = {(item["line"], item["column_start"], item["column_end"]):
                                  item.get("support_status", "unsupported") for item in issues}
                flagged = {claim["index"] for claim in case["claims"] if (
                    claim["line"], claim["column_start"], claim["column_end"]) in locations}
                children = {}
                child_scenarios = {}
                for claim in case["missed_claims"]:
                    if claim["parent_claim_index"] is not None:
                        children.setdefault(claim["parent_claim_index"], []).append(claim["text"])
                        child_scenarios.setdefault(claim["parent_claim_index"], set()).update(claim["scenarios"])
                        nested_claims += 1
                for claim in case["claims"]:
                    detected = claim["index"] in flagged
                    location = (claim["line"], claim["column_start"], claim["column_end"])
                    support_status = issue_statuses.get(location, "supported")
                    decision_counts[support_status] += 1
                    unsupported = claim["label"] == "unsupported"
                    key = ("true_positive" if unsupported else "false_positive") if detected else (
                        "false_negative" if unsupported else "true_negative")
                    counts[key] += 1
                    scenarios = set(claim["scenarios"]) | child_scenarios.get(claim["index"], set())
                    if not scenarios:
                        untagged += 1
                    for scenario in scenarios:
                        scenario_counts[scenario]["items"] += 1
                        if key in {"false_positive", "false_negative"}:
                            scenario_counts[scenario][key] += 1
                    events.append({"report_id": report_id, "kind": "auto", "claim_index": claim["index"],
                                   "line": claim["line"], "type": key,
                                   "nested_count": len(children.get(claim["index"], [])),
                                   "support_status": support_status})
                    if key in {"false_positive", "false_negative"}:
                        mismatches.append({"report_id": report_id, "claim_index": claim["index"],
                                           "line": claim["line"], "text": claim["text"], "type": key,
                                           "child_texts": children.get(claim["index"], [])})
                for claim in case["missed_claims"]:
                    if claim["parent_claim_index"] is not None:
                        continue
                    counts["false_negative"] += 1
                    if not claim["scenarios"]:
                        untagged += 1
                    for scenario in claim["scenarios"]:
                        scenario_counts[scenario]["items"] += 1
                        scenario_counts[scenario]["false_negative"] += 1
                    events.append({"report_id": report_id, "kind": "manual", "claim_index": claim["index"],
                                   "line": claim["line"], "type": "false_negative", "nested_count": 0})
                    mismatches.append({"report_id": report_id, "claim_index": claim["index"],
                                       "line": claim["line"], "text": claim["text"], "type": "false_negative",
                                       "child_texts": []})
            labeled_claims = sum(counts.values())
            ready = len(corpus) >= 3 and labeled_claims >= 10
            precision_denominator = counts["true_positive"] + counts["false_positive"]
            recall_denominator = counts["true_positive"] + counts["false_negative"]
            corpus_fingerprint = hashlib.sha256(json.dumps(
                corpus, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            return {"status": "ready" if ready else "insufficient", "report_count": len(corpus),
                    "labeled_claims": labeled_claims, "nested_claims": nested_claims,
                    "project_count": len(project_ids),
                    "stale_reports": stale_reports, "counts": counts,
                    "decision_counts": decision_counts,
                    "scenario_coverage": {"scenarios": scenario_counts, "untagged": untagged},
                    "precision": round(counts["true_positive"] / precision_denominator, 3)
                    if ready and precision_denominator else None,
                    "recall": round(counts["true_positive"] / recall_denominator, 3)
                    if ready and recall_denominator else None,
                    "mismatches": mismatches, "events": events,
                    "corpus_fingerprint": corpus_fingerprint}

    def review_benchmark(self) -> Dict[str, Any]:
        result = self._review_benchmark_snapshot(CRITIC_MATCH_THRESHOLD_PERCENT,
                                                 self._critic_validation_ids())
        result.pop("events")
        return result

    def _critic_validation_ids(self) -> Optional[List[str]]:
        with self._connection() as connection:
            latest = connection.execute("SELECT version FROM critic_benchmarks ORDER BY version DESC LIMIT 1").fetchone()
            if latest is None:
                return None
            return [row["report_id"] for row in connection.execute(
                """SELECT report_id FROM critic_benchmark_members
                WHERE version=? AND split='validation' ORDER BY report_id""", (latest["version"],)
            )]

    def critic_benchmark_status(self) -> Dict[str, Any]:
        with self._connection() as connection:
            latest = connection.execute("SELECT * FROM critic_benchmarks ORDER BY version DESC LIMIT 1").fetchone()
            if latest is None:
                return {"version": None, "state": "not_frozen", "holdout_reports": 0,
                        "holdout_claims": 0, "holdout_projects": 0}
            holdout_ids = [row["report_id"] for row in connection.execute(
                """SELECT report_id FROM critic_benchmark_members
                WHERE version=? AND split='holdout' ORDER BY report_id""", (latest["version"],)
            )]
        if latest["holdout_used_at"]:
            result = json.loads(latest["holdout_result_json"])
            return {"version": latest["version"], "state": "used", "result": result,
                    "holdout_reports": result["report_count"], "holdout_claims": result["labeled_claims"],
                    "holdout_projects": result["project_count"]}
        holdout = self._review_benchmark_snapshot(CRITIC_MATCH_THRESHOLD_PERCENT, holdout_ids)
        return {"version": latest["version"], "state": "collecting",
                "validation_stale": self.review_benchmark()["corpus_fingerprint"] != latest["validation_fingerprint"],
                "holdout_reports": holdout["report_count"], "holdout_claims": holdout["labeled_claims"],
                "holdout_projects": holdout["project_count"],
                "holdout_ready": (holdout["report_count"] >= 2 and holdout["labeled_claims"] >= 6
                                  and holdout["project_count"] >= 2 and
                                  holdout["stale_reports"] == 0 and
                                  holdout["counts"]["true_positive"] + holdout["counts"]["false_negative"] >= 2 and
                                  holdout["counts"]["false_positive"] + holdout["counts"]["true_negative"] >= 2)}

    def create_critic_benchmark(self) -> Dict[str, Any]:
        with self._lock:
            with self._connection() as connection:
                current = connection.execute(
                    "SELECT holdout_used_at FROM critic_benchmarks ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if current and current["holdout_used_at"] is None and not self.critic_benchmark_status()["validation_stale"]:
                    raise ValueError("现有审查基准仍在收集 Holdout")
                all_ids = [row["report_id"] for row in connection.execute(
                    "SELECT report_id FROM review_claim_labels UNION SELECT report_id FROM review_missed_claims ORDER BY report_id"
                )]
                previously_used = {row["report_id"] for row in connection.execute(
                    "SELECT DISTINCT report_id FROM critic_benchmark_members WHERE split IN ('validation','holdout')"
                )}
            validation_ids = []
            for report_id in all_ids:
                if report_id in previously_used:
                    continue
                try:
                    if self.get_review_label_case(report_id)["labeled"]:
                        validation_ids.append(report_id)
                except ValueError:
                    continue
            baseline = self._review_benchmark_snapshot(CRITIC_MATCH_THRESHOLD_PERCENT, validation_ids)
            counts = baseline["counts"]
            if (baseline["status"] != "ready" or baseline["project_count"] < 2 or
                counts["true_positive"] + counts["false_negative"] < 2 or
                counts["false_positive"] + counts["true_negative"] < 2):
                raise ValueError("Validation 需至少 3 份、10 项、2 个项目，且有来源和无来源各至少 2 项")
            with self._connection() as connection:
                version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM critic_benchmarks").fetchone()[0]
                connection.execute(
                    "INSERT INTO critic_benchmarks(version,validation_fingerprint,created_at) VALUES(?,?,?)",
                    (version, baseline["corpus_fingerprint"], utc_now()),
                )
                connection.executemany(
                    "INSERT INTO critic_benchmark_members(version,report_id,split) VALUES(?,?,?)",
                    [(version, report_id, "validation" if report_id in validation_ids else "excluded")
                     for report_id in all_ids],
                )
            return self.critic_benchmark_status()

    def list_critic_rule_versions(self) -> Dict[str, Any]:
        benchmark = self.review_benchmark()
        current_fingerprint = benchmark["corpus_fingerprint"]
        with self._connection() as connection:
            versions = [_critic_rule(row) for row in connection.execute(
                "SELECT * FROM critic_rule_versions ORDER BY version DESC"
            )]
            evaluations = [dict(row) for row in connection.execute(
                """SELECT e.* FROM critic_rule_evaluations AS e WHERE e.rowid=(
                    SELECT x.rowid FROM critic_rule_evaluations AS x
                    WHERE x.candidate_version=e.candidate_version
                    ORDER BY (x.corpus_fingerprint=?) DESC,x.created_at DESC LIMIT 1
                ) ORDER BY e.created_at DESC""", (current_fingerprint,)
            )]
        by_version = {item["version"]: item for item in versions}
        results = []
        for item in evaluations:
            rule = by_version[item["candidate_version"]]
            rule_stale = (rule["algorithm_version"] != CRITIC_MATCH_ALGORITHM_VERSION or
                          rule["fingerprint"] != _critic_rule_fingerprint(rule["skill"]))
            results.append(self._critic_rule_evaluation(item, current_fingerprint, rule_stale))
        return {"versions": versions, "evaluations": results,
                "active_version": self.active_critic_rule()["version"],
                "switches": self.list_critic_rule_switches(),
                "monitor": self.critic_rule_monitor(),
                "corpus_fingerprint": current_fingerprint,
                "frozen_benchmark": self.critic_benchmark_status(),
                "benchmark": {key: benchmark[key] for key in (
                    "status", "report_count", "labeled_claims", "stale_reports", "project_count",
                    "counts", "decision_counts", "scenario_coverage")}}

    @staticmethod
    def _critic_rule_evaluation(row: Dict[str, Any], current_fingerprint: str,
                                rule_stale: bool = False) -> Dict[str, Any]:
        item = dict(row)
        item["baseline"] = json.loads(item.pop("baseline_json"))
        item["candidate"] = json.loads(item.pop("candidate_json"))
        item["changes"] = json.loads(item.pop("changes_json"))
        item["stale"] = rule_stale or item["corpus_fingerprint"] != current_fingerprint
        return item

    def create_critic_rule_version(
        self, threshold_percent: int, ngram_overlap_weight_percent: int = 0,
        semantic_weight_percent: int = 0, uncertainty_margin_percent: int = 0,
        source: str = "manual", proposal: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if source not in {"manual", "validation_proposal"}:
            raise ValueError("Critic Skill 来源无效")
        schema_version = 2 if semantic_weight_percent or uncertainty_margin_percent else 1
        skill = _critic_skill(
            threshold_percent, ngram_overlap_weight_percent,
            semantic_weight_percent, uncertainty_margin_percent, schema_version,
        )
        if skill["claim_match"] == _critic_skill(
            CRITIC_MATCH_THRESHOLD_PERCENT, schema_version=0,
        )["claim_match"]:
            raise ValueError("候选 Skill 与当前内置规则相同")
        fingerprint = _critic_rule_fingerprint(skill)
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM critic_rule_versions WHERE claim_threshold_percent=?",
                (threshold_percent,),
            ).fetchone():
                raise ValueError("相同匹配阈值的 Critic Skill 已存在")
            if connection.execute(
                "SELECT 1 FROM critic_rule_versions WHERE fingerprint=?", (fingerprint,)
            ).fetchone():
                raise ValueError("相同规则的候选版本已存在")
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM critic_rule_versions").fetchone()[0]
            connection.execute(
                """INSERT INTO critic_rule_versions(
                    version,claim_threshold_percent,algorithm_version,fingerprint,skill_json,
                    proposal_json,source,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (version, threshold_percent, CRITIC_MATCH_ALGORITHM_VERSION,
                 fingerprint, json.dumps(skill, ensure_ascii=False, sort_keys=True),
                 json.dumps(proposal or {}, ensure_ascii=False, sort_keys=True), source, utc_now()),
            )
            row = connection.execute("SELECT * FROM critic_rule_versions WHERE version=?", (version,)).fetchone()
        return _critic_rule(row)

    def propose_critic_skill(self) -> Dict[str, Any]:
        validation_ids = self._critic_validation_ids()
        if validation_ids is None:
            raise ValueError("请先冻结审查 Validation，再生成 Critic Skill 候选")
        baseline_skill = _critic_skill(CRITIC_MATCH_THRESHOLD_PERCENT, schema_version=0)
        baseline = self._review_benchmark_snapshot(baseline_skill, validation_ids)
        counts = baseline["counts"]
        positive = counts["true_positive"] + counts["false_negative"]
        negative = counts["false_positive"] + counts["true_negative"]
        if baseline["status"] != "ready" or positive < 2 or negative < 2:
            raise ValueError("审查标注样本不足：至少 3 篇报告、10 项事项，且有来源和无来源各至少 2 项")
        with self._connection() as connection:
            used_thresholds = {row[0] for row in connection.execute(
                "SELECT claim_threshold_percent FROM critic_rule_versions"
            )}
        candidates = []
        evaluated = 0
        for threshold in range(40, 81, 5):
            if threshold in used_thresholds:
                continue
            for weight in (0, 25):
                for semantic_weight in (30, 60):
                    for uncertainty_margin in (5, 10):
                        evaluated += 1
                        skill = _critic_skill(
                            threshold, weight, semantic_weight, uncertainty_margin, schema_version=2,
                        )
                        result = self._review_benchmark_snapshot(skill, validation_ids)
                        candidate_counts = result["counts"]
                        improved = (
                            candidate_counts["false_positive"] <= counts["false_positive"] and
                            candidate_counts["false_negative"] <= counts["false_negative"] and
                            (candidate_counts["false_positive"] < counts["false_positive"] or
                             candidate_counts["false_negative"] < counts["false_negative"])
                        )
                        if improved:
                            candidates.append((
                                candidate_counts["false_positive"] + candidate_counts["false_negative"],
                                candidate_counts["false_negative"], candidate_counts["false_positive"],
                                result["decision_counts"]["uncertain"],
                                abs(threshold - CRITIC_MATCH_THRESHOLD_PERCENT), semantic_weight,
                                uncertainty_margin, weight, skill, result,
                            ))
        if not candidates:
            raise ValueError("当前 Validation 没有找到误报和漏报均不增加的可用 Skill 候选")
        selected = min(candidates, key=lambda item: item[:8])
        skill = selected[8]
        candidate = selected[9]
        proposal = {
            "corpus_fingerprint": baseline["corpus_fingerprint"],
            "validation_report_ids": list(validation_ids),
            "searched_candidates": evaluated,
            "baseline_counts": counts, "candidate_counts": candidate["counts"],
            "scenario_coverage": baseline["scenario_coverage"],
            "reason": "在限定结构化语义参数网格中选择误报与漏报均不增加、总错误最少的候选。",
        }
        created = self.create_critic_rule_version(
            skill["claim_match"]["threshold_percent"],
            skill["claim_match"]["ngram_overlap_weight_percent"],
            skill["claim_match"]["semantic_weight_percent"],
            skill["claim_match"]["uncertainty_margin_percent"],
            source="validation_proposal", proposal=proposal,
        )
        evaluation = self.evaluate_critic_rule_version(created["version"])
        return {"version": created, "evaluation": evaluation}

    def evaluate_critic_rule_version(self, version: int) -> Dict[str, Any]:
        with self._lock:
            with self._connection() as connection:
                row = connection.execute("SELECT * FROM critic_rule_versions WHERE version=?", (version,)).fetchone()
            if row is None or row["source"] not in {"manual", "validation_proposal"}:
                raise ValueError("候选规则版本不存在")
            rule = _critic_rule(row)
            if (rule["algorithm_version"] != CRITIC_MATCH_ALGORITHM_VERSION or
                    rule["fingerprint"] != _critic_rule_fingerprint(rule["skill"])):
                raise ValueError("候选规则定义已变化，不能继续评测")
            validation_ids = self._critic_validation_ids()
            baseline = self._review_benchmark_snapshot(CRITIC_MATCH_THRESHOLD_PERCENT, validation_ids)
            baseline_counts = baseline["counts"]
            positive = baseline_counts["true_positive"] + baseline_counts["false_negative"]
            negative = baseline_counts["false_positive"] + baseline_counts["true_negative"]
            if baseline["status"] != "ready" or positive < 2 or negative < 2:
                raise ValueError("审查标注样本不足：至少 3 篇报告、10 项事项，且有来源和无来源各至少 2 项")
            with self._connection() as connection:
                existing = connection.execute(
                    """SELECT * FROM critic_rule_evaluations
                    WHERE candidate_version=? AND corpus_fingerprint=?""",
                    (version, baseline["corpus_fingerprint"]),
                ).fetchone()
            if existing:
                return self._critic_rule_evaluation(dict(existing), baseline["corpus_fingerprint"])
            candidate = self._review_benchmark_snapshot(rule["skill"], validation_ids)
            if candidate["corpus_fingerprint"] != baseline["corpus_fingerprint"]:
                raise ValueError("人工标注在评测过程中发生变化，请重试")
            candidate_counts = candidate["counts"]
            improved = (candidate_counts["false_positive"] <= baseline_counts["false_positive"] and
                        candidate_counts["false_negative"] <= baseline_counts["false_negative"] and
                        (candidate_counts["false_positive"] < baseline_counts["false_positive"] or
                         candidate_counts["false_negative"] < baseline_counts["false_negative"]))
            previous = {(item["report_id"], item["kind"], item["claim_index"]): item
                        for item in baseline["events"]}
            changes = [{"report_id": item["report_id"], "kind": item["kind"],
                        "claim_index": item["claim_index"], "line": item["line"],
                        "before": previous[key]["type"], "after": item["type"],
                        "nested_count": item["nested_count"]}
                       for item in candidate["events"]
                       if (key := (item["report_id"], item["kind"], item["claim_index"])) in previous
                       and previous[key]["type"] != item["type"]]
            def summary(result: Dict[str, Any]) -> Dict[str, Any]:
                return {key: result[key] for key in (
                    "report_count", "labeled_claims", "nested_claims", "counts", "decision_counts",
                    "precision", "recall")}
            evaluation_id = str(uuid.uuid4())
            with self._connection() as connection:
                connection.execute(
                    """INSERT INTO critic_rule_evaluations(
                        id,candidate_version,corpus_fingerprint,baseline_json,candidate_json,
                        changes_json,decision,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (evaluation_id, version, baseline["corpus_fingerprint"],
                     json.dumps(summary(baseline), ensure_ascii=False),
                     json.dumps(summary(candidate), ensure_ascii=False),
                     json.dumps(changes, ensure_ascii=False),
                     "improved" if improved else "not_improved", utc_now()),
                )
                saved = connection.execute("SELECT * FROM critic_rule_evaluations WHERE id=?", (
                    evaluation_id,
                )).fetchone()
            return self._critic_rule_evaluation(dict(saved), baseline["corpus_fingerprint"])

    def gate_critic_rule_version(self, version: int) -> Dict[str, Any]:
        with self._lock:
            with self._connection() as connection:
                benchmark = connection.execute(
                    "SELECT * FROM critic_benchmarks ORDER BY version DESC LIMIT 1"
                ).fetchone()
                rule_row = connection.execute(
                    """SELECT * FROM critic_rule_versions WHERE version=?
                    AND source IN ('manual','validation_proposal')""", (version,)
                ).fetchone()
                if benchmark is None or rule_row is None:
                    raise ValueError("审查基准或候选规则不存在")
                rule = _critic_rule(rule_row)
                if benchmark["holdout_used_at"]:
                    raise ValueError("本版审查 Holdout 已使用，不能重复门禁")
                if (rule["algorithm_version"] != CRITIC_MATCH_ALGORITHM_VERSION or
                    rule["fingerprint"] != _critic_rule_fingerprint(rule["skill"])):
                    raise ValueError("候选规则定义已变化")
                validation_ids = [row["report_id"] for row in connection.execute(
                    "SELECT report_id FROM critic_benchmark_members WHERE version=? AND split='validation' ORDER BY report_id",
                    (benchmark["version"],),
                )]
                holdout_ids = [row["report_id"] for row in connection.execute(
                    "SELECT report_id FROM critic_benchmark_members WHERE version=? AND split='holdout' ORDER BY report_id",
                    (benchmark["version"],),
                )]
                evaluation = connection.execute(
                    """SELECT decision FROM critic_rule_evaluations
                    WHERE candidate_version=? AND corpus_fingerprint=?""",
                    (version, benchmark["validation_fingerprint"]),
                ).fetchone()
            validation = self._review_benchmark_snapshot(CRITIC_MATCH_THRESHOLD_PERCENT, validation_ids)
            if validation["corpus_fingerprint"] != benchmark["validation_fingerprint"]:
                raise ValueError("Validation 冻结样本已变化，请建立新基准")
            if evaluation is None or evaluation["decision"] != "improved":
                raise ValueError("候选须先通过当前冻结 Validation 对照")
            baseline = self._review_benchmark_snapshot(CRITIC_MATCH_THRESHOLD_PERCENT, holdout_ids)
            counts = baseline["counts"]
            if (baseline["report_count"] < 2 or baseline["labeled_claims"] < 6 or
                baseline["project_count"] < 2 or baseline["stale_reports"] or
                counts["true_positive"] + counts["false_negative"] < 2 or
                counts["false_positive"] + counts["true_negative"] < 2):
                raise ValueError("Holdout 需至少 2 份、6 项、2 个项目，且有来源和无来源各至少 2 项；失效标注需重标")
            candidate = self._review_benchmark_snapshot(rule["skill"], holdout_ids)
            if candidate["corpus_fingerprint"] != baseline["corpus_fingerprint"]:
                raise ValueError("Holdout 标注在评测期间发生变化")
            other = candidate["counts"]
            passed = (other["false_positive"] <= counts["false_positive"] and
                      other["false_negative"] <= counts["false_negative"])
            result = {"version": benchmark["version"], "candidate_version": version,
                      "decision": "passed" if passed else "failed", "report_count": baseline["report_count"],
                      "labeled_claims": baseline["labeled_claims"], "project_count": baseline["project_count"],
                      "holdout_fingerprint": baseline["corpus_fingerprint"],
                      "baseline_counts": counts, "candidate_counts": other}
            with self._connection() as connection:
                cursor = connection.execute(
                    """UPDATE critic_benchmarks SET holdout_used_at=?,holdout_result_json=?
                    WHERE version=? AND holdout_used_at IS NULL""",
                    (utc_now(), json.dumps(result, ensure_ascii=False), benchmark["version"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("本版审查 Holdout 已被领取")
            return result

    @staticmethod
    def _active_critic_rule_in_connection(connection: sqlite3.Connection) -> Dict[str, Any]:
        row = connection.execute(
            """SELECT r.* FROM critic_rule_versions r
            WHERE r.version=COALESCE((SELECT active_version FROM critic_rule_switches
            ORDER BY rowid DESC LIMIT 1),1)"""
        ).fetchone()
        if row is None:
            raise ValueError("活动 Critic 规则不存在")
        return _critic_rule(row)

    def critic_rule_for_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        binding = snapshot.get("critic_rules", {})
        version = binding.get("version", CRITIC_RULE_VERSION)
        if type(version) is not int:
            raise ValueError("Critic 规则版本无效")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM critic_rule_versions WHERE version=?", (version,)).fetchone()
        rule = _critic_rule(row) if row else None
        expected_fingerprint = _critic_rule_fingerprint(rule["skill"]) if rule else None
        if (row is None or row["algorithm_version"] != CRITIC_MATCH_ALGORITHM_VERSION or
            row["fingerprint"] != expected_fingerprint or
            binding.get("fingerprint", expected_fingerprint if version == 1 else None) != expected_fingerprint):
            raise ValueError("Critic 规则版本缺失或指纹不一致")
        return rule

    def active_critic_rule(self) -> Dict[str, Any]:
        with self._connection() as connection:
            row = self._active_critic_rule_in_connection(connection)
        self.critic_rule_for_snapshot({"critic_rules": {"version": row["version"],
                                                       "fingerprint": row["fingerprint"]}})
        return _critic_rule(row)

    def list_critic_rule_switches(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM critic_rule_switches ORDER BY rowid DESC"
            )]

    def activate_critic_rule(self, version: int) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._active_critic_rule_in_connection(connection)
            target = connection.execute("SELECT * FROM critic_rule_versions WHERE version=?", (version,)).fetchone()
            target_rule = _critic_rule(target) if target else None
            if (target is None or target["algorithm_version"] != CRITIC_MATCH_ALGORITHM_VERSION or
                    target["fingerprint"] != _critic_rule_fingerprint(target_rule["skill"])):
                raise ValueError("Critic 规则版本不存在或定义已变化")
            if current["version"] == version:
                return _critic_rule(current)
            was_active = version == 1 or connection.execute(
                "SELECT 1 FROM critic_rule_switches WHERE active_version=? LIMIT 1", (version,)
            ).fetchone() is not None
            if not was_active:
                benchmark = connection.execute(
                    "SELECT holdout_result_json FROM critic_benchmarks ORDER BY version DESC LIMIT 1"
                ).fetchone()
                result = json.loads(benchmark["holdout_result_json"]) if benchmark and benchmark["holdout_result_json"] else None
                if current["version"] != 1 or not result or result["decision"] != "passed" or result["candidate_version"] != version:
                    raise ValueError("候选须先相对内置规则通过最新审查 Holdout 门禁")
            connection.execute(
                """INSERT INTO critic_rule_switches(id,previous_version,active_version,source,created_at)
                VALUES(?,?,?,?,?)""",
                (str(uuid.uuid4()), current["version"], version,
                 "rollback" if was_active else "activation", utc_now()),
            )
        return self.active_critic_rule()

    def critic_rule_monitor(self) -> Dict[str, Any]:
        with self._connection() as connection:
            switch = connection.execute(
                "SELECT * FROM critic_rule_switches ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if switch is None or switch["active_version"] == 1:
                return {"status": "inactive", "report_count": 0, "labeled_claims": 0}
            report_ids = [row["report_id"] for row in connection.execute(
                """SELECT DISTINCT r.id AS report_id FROM reports r
                JOIN evaluation_cases c ON c.report_id=r.id AND c.split='validation'
                JOIN review_claim_labels l ON l.report_id=r.id
                WHERE r.kind='daily' AND r.origin='generated' AND r.status='confirmed'
                AND r.updated_at>? AND NOT EXISTS (
                    SELECT 1 FROM critic_benchmark_members m WHERE m.report_id=r.id)
                ORDER BY r.id""", (switch["created_at"],),
            )]
            previous = connection.execute(
                "SELECT * FROM critic_rule_versions WHERE version=?",
                (switch["previous_version"],),
            ).fetchone()
            active = connection.execute(
                "SELECT * FROM critic_rule_versions WHERE version=?",
                (switch["active_version"],),
            ).fetchone()
        if previous is None or active is None:
            raise ValueError("监测规则版本不存在")
        for rule in (previous, active):
            self.critic_rule_for_snapshot({"critic_rules": {"version": rule["version"],
                                                           "fingerprint": rule["fingerprint"]}})
        current = self._review_benchmark_snapshot(_critic_rule(active)["skill"], report_ids)
        before = self._review_benchmark_snapshot(_critic_rule(previous)["skill"], report_ids)
        return {"status": "ready" if current["status"] == "ready" and not current["stale_reports"] else "insufficient",
                "active_version": switch["active_version"], "previous_version": switch["previous_version"],
                "report_count": current["report_count"], "labeled_claims": current["labeled_claims"],
                "stale_reports": current["stale_reports"], "project_count": current["project_count"],
                "counts": current["counts"], "previous_counts": before["counts"]}

    def update_report_draft(self, report_id: str, draft: str) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE reports SET draft=?,updated_at=? WHERE id=? AND status='draft' AND origin='generated'",
                (draft, utc_now(), report_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("只能保存未确认的系统草稿")
            row = connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return self._report(row)

    def set_generated_draft(self, report_id: str, draft: str) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE reports SET draft=?,initial_draft=?,updated_at=? WHERE id=? AND status='draft' AND origin='generated'",
                (draft, draft, utc_now(), report_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("只能更新未确认的系统初稿")
            row = connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return self._report(row)

    def list_reports(
        self, kind: str = "", start: str = "", end: str = "", project_id: str = ""
    ) -> List[Dict[str, Any]]:
        clauses = []
        values: List[str] = []
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        if start:
            clauses.append("report_date>=?")
            values.append(start)
        if end:
            clauses.append("report_date<=?")
            values.append(end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM reports" + where + " ORDER BY report_date DESC, created_at DESC", tuple(values)
            ).fetchall()
        reports = [self._report(row) for row in rows]
        if project_id:
            reports = [report for report in reports if any(
                isinstance(project, dict) and project.get("id") == project_id
                for project in (report["source"].get("projects") or [])
            )]
        return reports

    def confirm_report(self, report_id: str, final_text: str) -> Dict[str, Any]:
        final_text = final_text.strip()
        if not final_text:
            raise ValueError("终稿不能为空")
        with self._lock, self._connection() as connection:
            self._confirm_report_in_transaction(connection, report_id, final_text)
        return self.get_report(report_id)

    @staticmethod
    def _confirm_report_in_transaction(connection: sqlite3.Connection, report_id: str, final_text: str) -> None:
        row = connection.execute(
            "SELECT source_json,draft,initial_draft,status,origin FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise ValueError("报告不存在")
        if row["status"] != "draft" or row["origin"] != "generated":
            raise ValueError("只能确认未确认的系统草稿")
        now = utc_now()
        connection.execute(
            "UPDATE reports SET final_text=?,status='confirmed',updated_at=? WHERE id=?",
            (final_text, now, report_id),
        )
        case_id = str(uuid.uuid4())
        split = "holdout" if int(case_id.replace("-", "")[:2], 16) % 5 == 0 else "validation"
        connection.execute(
            """INSERT INTO evaluation_cases(
                id,report_id,split,source_json,expected_text,created_at
            ) VALUES(?,?,?,?,?,?)""",
            (case_id, report_id, split, row["source_json"], final_text, now),
        )
        if json.loads(row["source_json"]).get("kind") == "daily":
            signals = summarize_edit_feedback(row["initial_draft"] or row["draft"], final_text)
            connection.execute(
                "INSERT INTO writing_feedback(report_id,split,signals_json,created_at) VALUES(?,?,?,?)",
                (report_id, split, json.dumps(signals, ensure_ascii=False), now),
            )

    def list_writing_feedback(self, split: str = "validation") -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM writing_feedback WHERE split=? ORDER BY created_at", (split,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["signals"] = json.loads(item.pop("signals_json"))
            result.append(item)
        return result

    def add_memory(
        self, scope: str, memory_key: str, value: str, project_id: Optional[str] = None,
        source_type: str = "user", source_id: Optional[str] = None,
        supersedes_id: Optional[str] = None, expires_on: Optional[str] = None,
    ) -> Dict[str, Any]:
        if scope == "personal":
            if expires_on is not None:
                raise ValueError("个人写作偏好不支持到期日期")
            if project_id is not None or memory_key not in {"numbered", "include_statistics", "max_item_length"}:
                raise ValueError("个人偏好字段无效")
            if memory_key == "max_item_length":
                if not value.isdigit() or not 40 <= int(value) <= 160:
                    raise ValueError("单条长度需在 40 到 160 字之间")
            elif value not in {"true", "false"}:
                raise ValueError("该偏好只能填写 true 或 false")
        elif scope == "project":
            if not project_id or memory_key != "note" or not value.strip() or len(value) > 300:
                raise ValueError("项目备注需关联项目，且不超过 300 字")
            expires_on = _validated_memory_expiry(expires_on)
        else:
            raise ValueError("记忆范围无效")
        if source_type not in {"user", "user_correction", "evaluation", "archive_candidate"}:
            raise ValueError("记忆来源无效")
        from .report_generator import redact_sensitive

        memory_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connection() as connection:
            if project_id and connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise ValueError("项目不存在")
            if supersedes_id:
                previous = connection.execute(
                    "SELECT scope,project_id,memory_key,status FROM memory_entries WHERE id=?", (supersedes_id,)
                ).fetchone()
                if previous is None or previous["status"] != "active" or (
                    previous["scope"], previous["project_id"], previous["memory_key"]
                ) != (scope, project_id, memory_key):
                    raise ValueError("要纠正的记忆不存在或范围不匹配")
                connection.execute(
                    "UPDATE memory_entries SET status='corrected',updated_at=? WHERE id=?", (now, supersedes_id)
                )
            elif scope == "personal" and source_type == "user":
                connection.execute(
                    """UPDATE memory_entries SET status='corrected',updated_at=?
                    WHERE scope='personal' AND memory_key=? AND status='active' AND source_type!='evaluation'""",
                    (now, memory_key),
                )
            connection.execute(
                """INSERT INTO memory_entries(
                    id,scope,project_id,memory_key,value,source_type,source_id,supersedes_id,status,
                    created_at,updated_at,expires_on
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, scope, project_id, memory_key, redact_sensitive(value.strip()), source_type,
                 source_id, supersedes_id, "active", now, now, expires_on),
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM memory_entries WHERE id=?", (memory_id,)).fetchone()
        return _memory_entry(row) if row else None

    def list_memories(self, active_only: bool = True, include_expired: bool = False) -> List[Dict[str, Any]]:
        today = _memory_today().isoformat()
        if active_only:
            where = " WHERE status='active'"
            if not include_expired:
                where += " AND (expires_on IS NULL OR expires_on>=?)"
        else:
            where = ""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_entries" + where + " ORDER BY created_at DESC,id DESC",
                (today,) if active_only and not include_expired else (),
            ).fetchall()
        return [_memory_entry(row, today) for row in rows]

    def correct_memory(
        self, memory_id: str, value: str, expires_on: Optional[str] = None,
        update_expiry: bool = False,
    ) -> Dict[str, Any]:
        previous = self.get_memory(memory_id)
        if previous is None or previous["status"] != "active":
            raise ValueError("要纠正的记忆不存在")
        return self.add_memory(
            previous["scope"], previous["memory_key"], value, previous["project_id"],
            "user_correction", memory_id, memory_id,
            expires_on if update_expiry else previous["expires_on"],
        )

    def remove_memory(self, memory_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE memory_entries SET status='removed',updated_at=? WHERE id=? AND status='active'",
                (utc_now(), memory_id),
            )
        return cursor.rowcount == 1

    def memory_context(self, project_ids: Sequence[str]) -> Dict[str, Any]:
        active = self.list_memories()
        preferences: Dict[str, Any] = {}
        preference_sources: Dict[str, str] = {}
        ranked = sorted(
            (item for item in active if item["scope"] == "personal" and item["source_type"] != "evaluation"),
            key=lambda item: item["created_at"],
            reverse=True,
        )
        for item in ranked:
            key = item["memory_key"]
            if key in preferences:
                continue
            preferences[key] = int(item["value"]) if key == "max_item_length" else item["value"] == "true"
            preference_sources[key] = item["id"]
        selected_projects = set(project_ids)
        project_notes = [
            {"id": item["id"], "project_id": item["project_id"], "text": item["value"]}
            for item in active if item["scope"] == "project" and item["project_id"] in selected_projects
        ][:20]
        guidance = _project_memory_guidance(project_notes)
        return {"preferences": preferences, "preference_sources": preference_sources,
                "project_notes": project_notes, "project_guidance": guidance}

    def list_memory_suggestions(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT c.report_id,r.report_date,c.source_json FROM evaluation_cases AS c
                JOIN reports AS r ON r.id=c.report_id
                WHERE c.split='validation' AND r.status='confirmed' AND r.origin='generated'
                AND r.kind='daily' ORDER BY r.report_date,c.report_id"""
            ).fetchall()
            project_ids = {row["id"] for row in connection.execute("SELECT id FROM projects")}
            decided = {row["suggestion_id"] for row in connection.execute(
                "SELECT suggestion_id FROM memory_suggestion_decisions")}
            personal_decided = {row["suggestion_id"] for row in connection.execute(
                "SELECT suggestion_id FROM personal_memory_suggestion_decisions")}
            memories = [_memory_entry(row) for row in connection.execute(
                "SELECT * FROM memory_entries ORDER BY created_at DESC,id DESC"
            ).fetchall()]
            feedback_rows = connection.execute(
                """SELECT w.report_id,w.signals_json,r.report_date FROM writing_feedback AS w
                JOIN evaluation_cases AS c ON c.report_id=w.report_id
                JOIN reports AS r ON r.id=w.report_id
                WHERE w.split='validation' AND c.split='validation' AND r.status='confirmed'
                AND r.origin='generated' AND r.kind='daily' ORDER BY r.report_date,w.report_id"""
            ).fetchall()
        cases = [{"report_id": row["report_id"], "report_date": row["report_date"],
                  "source": json.loads(row["source_json"])} for row in rows]
        feedback = [{"report_id": row["report_id"], "report_date": row["report_date"],
                     "signals": json.loads(row["signals_json"])} for row in feedback_rows]
        active = [item for item in memories if item["status"] == "active" and not item["is_expired"]]
        expired = [item for item in memories if item["scope"] == "project" and item["is_expired"]]
        suggestions = []
        for item in suggest_project_memories(cases):
            if item["project_id"] not in project_ids or item["id"] in decided:
                continue
            project_active = [memory for memory in active if memory["scope"] == "project" and
                              memory["project_id"] == item["project_id"]]
            conflicts = [memory for memory in project_active
                         if memories_conflict(memory["value"], item["value"])]
            related = [memory for memory in project_active if not memories_conflict(
                       memory["value"], item["value"]) and
                       memory["project_id"] == item["project_id"] and
                       memory_similarity(memory["value"], item["value"]) >= 0.72]
            if any(normalized_memory_text(memory["value"]) == normalized_memory_text(item["value"])
                   for memory in related):
                continue
            item["merge_with"] = [_memory_reference(memory, item["value"]) for memory in related]
            item["conflicts_with"] = [_memory_reference(memory, item["value"]) for memory in conflicts]
            item["expired_matches"] = [_memory_reference(memory, item["value"]) for memory in expired
                                       if memory["project_id"] == item["project_id"] and
                                       memory_similarity(memory["value"], item["value"]) >= 0.72]
            suggestions.append(item)
        for item in suggest_personal_memories(feedback):
            if item["id"] in personal_decided:
                continue
            same_key = [memory for memory in active if memory["scope"] == "personal" and
                        memory["source_type"] != "evaluation" and memory["memory_key"] == item["memory_key"]]
            if any(memory["value"] == item["value"] for memory in same_key):
                continue
            item["merge_with"] = []
            item["conflicts_with"] = [_memory_reference(memory, item["value"]) for memory in same_key]
            item["expired_matches"] = []
            suggestions.append(item)
        return sorted(suggestions, key=lambda item: (item["scope"], item.get("project_id") or "",
                                                      item["memory_key"], item["value"]))

    def decide_memory_suggestion(
        self, suggestion_id: str, action: str, expires_on: Optional[str] = None
    ) -> Dict[str, Any]:
        if action not in {"accept", "ignore"}:
            raise ValueError("候选处理方式无效")
        if action == "ignore" and expires_on is not None:
            raise ValueError("忽略候选时不能设置到期日期")
        with self._lock:
            candidate = next((item for item in self.list_memory_suggestions()
                              if item["id"] == suggestion_id), None)
            if candidate is None:
                raise ValueError("候选不存在或已处理")
            if candidate["scope"] == "personal" and expires_on is not None:
                raise ValueError("个人写作偏好不支持到期日")
            expiry = (_validated_memory_expiry(expires_on)
                      if action == "accept" and candidate["scope"] == "project" else None)
            replace = list({item["id"]: item for item in
                            candidate["merge_with"] + candidate["conflicts_with"]}.values())
            memory_id = str(uuid.uuid4()) if action == "accept" else None
            now = utc_now()
            with self._connection() as connection:
                decision_table = ("personal_memory_suggestion_decisions"
                                  if candidate["scope"] == "personal" else "memory_suggestion_decisions")
                if connection.execute(f"SELECT 1 FROM {decision_table} WHERE suggestion_id=?",
                                      (suggestion_id,)).fetchone():
                    raise ValueError("候选已处理")
                if memory_id:
                    supersedes_id = replace[0]["id"] if replace else None
                    for replaced in replace:
                        cursor = connection.execute(
                            "UPDATE memory_entries SET status='corrected',updated_at=? WHERE id=? AND status='active'",
                            (now, replaced["id"]),
                        )
                        if cursor.rowcount != 1:
                            raise ValueError("要合并或纠正的记忆已发生变化")
                    connection.execute(
                        """INSERT INTO memory_entries(
                            id,scope,project_id,memory_key,value,source_type,source_id,supersedes_id,status,
                            created_at,updated_at,expires_on
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (memory_id, candidate["scope"], candidate.get("project_id"),
                         candidate["memory_key"], redact_sensitive(candidate["value"]),
                         "archive_candidate", suggestion_id, supersedes_id, "active", now, now, expiry),
                    )
                if candidate["scope"] == "personal":
                    connection.execute(
                        """INSERT INTO personal_memory_suggestion_decisions(
                            suggestion_id,status,memory_id,evidence_json,created_at
                        ) VALUES(?,?,?,?,?)""",
                        (suggestion_id, "accepted" if memory_id else "ignored", memory_id,
                         json.dumps(candidate["evidence"], ensure_ascii=False), now),
                    )
                else:
                    connection.execute(
                        """INSERT INTO memory_suggestion_decisions(
                            suggestion_id,project_id,status,memory_id,evidence_json,created_at
                        ) VALUES(?,?,?,?,?,?)""",
                        (suggestion_id, candidate["project_id"],
                         "accepted" if memory_id else "ignored", memory_id,
                         json.dumps(candidate["evidence"], ensure_ascii=False), now),
                    )
        return {"status": "accepted" if memory_id else "ignored",
                "memory": self.get_memory(memory_id) if memory_id else None}

    def list_evaluation_cases(self, split: str = "") -> List[Dict[str, Any]]:
        query = "SELECT * FROM evaluation_cases"
        values: tuple = ()
        if split:
            query += " WHERE split=?"
            values = (split,)
        query += " ORDER BY created_at"
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source"] = json.loads(item.pop("source_json"))
            result.append(item)
        return result

    def list_post_activation_validation_cases(self, strategy_version: int, activated_at: str) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT c.* FROM evaluation_cases c JOIN reports r ON r.id=c.report_id
                WHERE c.split='validation' AND r.kind='daily' AND r.origin='generated'
                AND r.strategy_version=? AND r.created_at>=? AND c.created_at>=?
                ORDER BY c.created_at,c.id""",
                (strategy_version, activated_at, activated_at),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source"] = json.loads(item.pop("source_json"))
            result.append(item)
        return result

    def evaluation_case_ids(self, split: str) -> List[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT id FROM evaluation_cases WHERE split=? ORDER BY id", (split,)).fetchall()
        return [row["id"] for row in rows]

    def list_evaluation_benchmarks(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM evaluation_benchmarks ORDER BY version DESC").fetchall()
        return [_benchmark(row) for row in rows]

    def validation_coverage(self, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        if any(item["split"] != "validation" for item in cases):
            raise ValueError("覆盖摘要仅支持 Validation 样本")
        with self._connection() as connection:
            return _validation_coverage(connection, cases)

    def create_evaluation_benchmark(self, min_validation: int, min_holdout: int) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM evaluation_benchmarks ORDER BY version DESC").fetchall()
            current = _benchmark(existing[0]) if existing else None
            used_holdout = {
                case_id for row in existing for case_id in json.loads(row["holdout_ids_json"])
            }
            used_validation = {
                case_id for row in existing for case_id in json.loads(row["validation_ids_json"])
            }
            rows = connection.execute(
                "SELECT id,report_id,split,source_json,expected_text FROM evaluation_cases ORDER BY id"
            ).fetchall()
            validation = [
                _benchmark_case(row) for row in rows
                if row["split"] == "validation" and row["id"] not in used_holdout
            ]
            holdout = [
                _benchmark_case(row) for row in rows
                if row["split"] == "holdout" and row["id"] not in used_holdout
                and row["id"] not in used_validation
            ]
            if len(validation) < min_validation or len(holdout) < min_holdout:
                raise ValueError("建立基准集需要足够的 Validation 和全新 Holdout 样本")
            validation_ids = [item["id"] for item in validation]
            if current and not set(validation_ids).difference(current["validation_ids"]):
                raise ValueError("新基准集还需要至少一份新增 Validation 样本")
            holdout_ids = [item["id"] for item in holdout]
            validation_hash = _case_fingerprint(validation)
            holdout_hash = _case_fingerprint(holdout)
            validation_draft_hash, _ = _prompt_input_snapshot(connection, validation)
            holdout_draft_hash, _ = _prompt_input_snapshot(connection, holdout)
            coverage = _validation_coverage(connection, validation)
            scenarios = {}
            for split, cases in (("validation", validation), ("holdout", holdout)):
                scenarios[split] = _case_scenarios(connection, cases)
            # Human annotations are exclusively drawn from Validation.
            for case in validation:
                if connection.execute(
                    "SELECT 1 FROM critic_benchmark_members WHERE report_id=? AND split='holdout' LIMIT 1",
                    (case["report_id"],),
                ).fetchone():
                    continue
                labeled = connection.execute(
                    "SELECT 1 FROM review_claim_labels WHERE report_id=? UNION SELECT 1 FROM review_missed_claims WHERE report_id=? LIMIT 1",
                    (case["report_id"], case["report_id"]),
                ).fetchone()
                if labeled:
                    label_case = self.get_review_label_case(case["report_id"])
                    if label_case["labeled"]:
                        tags = {tag for claim in label_case["claims"] + label_case["missed_claims"]
                                for tag in claim["scenarios"]}
                        scenarios["validation"][case["id"]].extend(f"review:{tag}" for tag in sorted(tags))
            scenarios_json = json.dumps(scenarios, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = _benchmark_fingerprint(
                validation_hash, holdout_hash, validation_draft_hash, holdout_draft_hash,
                scenarios_json,
            )
            version = current["version"] + 1 if current else 1
            connection.execute(
                """INSERT INTO evaluation_benchmarks(
                    version,validation_ids_json,holdout_ids_json,validation_hash,holdout_hash,
                    validation_draft_hash,holdout_draft_hash,coverage_json,scenarios_json,fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (version, json.dumps(validation_ids), json.dumps(holdout_ids), validation_hash,
                 holdout_hash, validation_draft_hash, holdout_draft_hash,
                 json.dumps(coverage, ensure_ascii=False), scenarios_json, fingerprint, utc_now()),
            )
            row = connection.execute("SELECT * FROM evaluation_benchmarks WHERE version=?", (version,)).fetchone()
        return _benchmark(row)

    def benchmark_cases(self, benchmark: Dict[str, Any], split: str) -> List[Dict[str, Any]]:
        if split not in {"validation", "holdout"}:
            raise ValueError("基准集分组无效")
        ids = benchmark[f"{split}_ids"]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT id,report_id,split,source_json,expected_text FROM evaluation_cases WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        cases = [_benchmark_case(row) for row in rows]
        if len(cases) != len(ids) or any(item["split"] != split for item in cases) or (
            _case_fingerprint(cases) != benchmark[f"{split}_hash"]
        ):
            raise ValueError("基准集快照与样本内容不一致，请检查本地数据库")
        return cases

    def benchmark_prompt_cases(self, benchmark: Dict[str, Any], split: str, limit: int) -> List[Dict[str, Any]]:
        cases = self.benchmark_cases(benchmark, split)
        if not cases:
            return []
        if not benchmark.get("validation_draft_hash") or not benchmark.get("holdout_draft_hash"):
            raise ValueError("旧基准未冻结初稿指纹，请使用新增样本建立新基准")
        with self._connection() as connection:
            snapshot_row = connection.execute("SELECT scenarios_json FROM evaluation_benchmarks WHERE version=?",
                                              (benchmark["version"],)).fetchone()
        scenarios_json = snapshot_row["scenarios_json"] if snapshot_row else None
        if _benchmark_fingerprint(
            benchmark["validation_hash"], benchmark["holdout_hash"],
            benchmark["validation_draft_hash"], benchmark["holdout_draft_hash"],
            scenarios_json,
        ) != benchmark["fingerprint"]:
            raise ValueError("基准集指纹不一致，请检查本地数据库")
        with self._connection() as connection:
            draft_hash, reports = _prompt_input_snapshot(connection, cases)
        if draft_hash != benchmark[f"{split}_draft_hash"]:
            raise ValueError("基准集初稿快照与当前报告不一致，请检查本地数据库")
        selected = []
        for case in sorted(cases, key=lambda item: item["id"]):
            report = reports.get(case["report_id"])
            if report and report["kind"] == "daily" and report["origin"] == "generated" and report["initial_draft"]:
                selected.append({**case, "initial_draft": report["initial_draft"]})
        if scenarios_json:
            tags = json.loads(scenarios_json)[split]
            return _stratified_cases(selected, tags, limit)
        return selected[:limit]

    def benchmark_prompt_validation_coverage(self, benchmark: Dict[str, Any], cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT scenarios_json FROM evaluation_benchmarks WHERE version=?",
                                     (benchmark["version"],)).fetchone()
        if not row or not row["scenarios_json"]:
            return {"available": [], "selected": [], "uncovered": [], "legacy": True}
        snapshots = json.loads(row["scenarios_json"])["validation"]
        available = {tag for tags in snapshots.values() if "eligible" in tags for tag in tags} - {"eligible"}
        selected = {tag for case in cases for tag in snapshots.get(case["id"], [])} - {"eligible"}
        return {"available": sorted(available), "selected": sorted(selected),
                "uncovered": sorted(available - selected), "legacy": False}

    def claim_benchmark_holdout(self, version: int) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE evaluation_benchmarks SET holdout_used_at=? WHERE version=?
                AND version=(SELECT MAX(version) FROM evaluation_benchmarks) AND holdout_used_at IS NULL""",
                (utc_now(), version),
            )
        return cursor.rowcount == 1

    def claim_benchmark_prompt_holdout(
        self, benchmark: Dict[str, Any], limit: int, evaluation_id: str, recovery_count: int,
    ) -> Optional[List[Dict[str, Any]]]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            evaluation = connection.execute(
                "SELECT stage,decision FROM writer_prompt_evaluations WHERE id=? AND recovery_count=?",
                (evaluation_id, recovery_count),
            ).fetchone()
            if evaluation is None or evaluation["stage"] != "validation" or evaluation["decision"] != "running":
                raise ValueError("Writer 指令评测阶段已变化")
            current = connection.execute(
                "SELECT version,fingerprint,holdout_used_at FROM evaluation_benchmarks ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if (not current or current["version"] != benchmark["version"] or
                    current["fingerprint"] != benchmark["fingerprint"] or current["holdout_used_at"]):
                return None
            cases = self.benchmark_prompt_cases(benchmark, "holdout", limit)
            if len(cases) < limit:
                raise ValueError("固定基准缺少真实生成并确认的 Holdout 日报")
            connection.execute(
                "UPDATE evaluation_benchmarks SET holdout_used_at=?,holdout_run_id=? WHERE version=?",
                (utc_now(), evaluation_id, benchmark["version"]),
            )
            connection.execute(
                "UPDATE writer_prompt_evaluations SET stage='holdout_claimed',updated_at=? WHERE id=?",
                (utc_now(), evaluation_id),
            )
        return cases

    def list_validation_critic_failures(self, strategy_version: int) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT s.run_id,r.report_id,s.output_json FROM agent_steps s
                JOIN agent_runs r ON r.id=s.run_id AND r.workflow='daily_report'
                JOIN reports p ON p.id=r.report_id AND p.strategy_version=?
                JOIN evaluation_cases c ON c.report_id=r.report_id AND c.split='validation'
                WHERE s.agent_name='Critic Agent' AND s.status='needs_review'
                ORDER BY s.created_at DESC LIMIT 50""",
                (strategy_version,),
            ).fetchall()
        return [
            {"run_id": row["run_id"], "report_id": row["report_id"], "review": json.loads(row["output_json"])}
            for row in rows
        ]

    def list_writer_prompt_versions(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT p.version,p.addendum,p.sha256,p.source,p.status,p.created_at,
                (p.version=1 OR EXISTS(SELECT 1 FROM writer_prompt_switches s
                WHERE s.active_version=p.version)) AS was_active
                FROM writer_prompt_versions p ORDER BY p.version DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_writer_prompt_switches(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM writer_prompt_switches ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_writer_prompt_switch(self) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM writer_prompt_switches ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def latest_writer_prompt_monitor_check(self) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM writer_prompt_monitor_checks ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def list_writer_prompt_monitor_cases(
        self, version: int, switched_at: str, *, after: bool, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        comparator = ">=" if after else "<"
        order = "ASC" if after else "DESC"
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT c.id AS case_id,c.report_id,c.source_json,c.expected_text,
                r.initial_draft,a.id AS run_id,a.instruction_json,a.created_at AS run_created_at
                FROM evaluation_cases c
                JOIN reports r ON r.id=c.report_id AND r.kind='daily'
                AND r.origin='generated' AND r.status='confirmed'
                JOIN agent_runs a ON a.report_id=r.id AND a.workflow='daily_report'
                AND a.status IN ('passed','needs_review')
                WHERE c.split='validation' AND a.created_at{comparator}?
                ORDER BY a.created_at {order},c.id {order}""",
                (switched_at,),
            ).fetchall()
            result = []
            for row in rows:
                snapshot = json.loads(row["instruction_json"])
                if snapshot.get("writer_prompt", {}).get("version") != version:
                    continue
                steps = connection.execute(
                    "SELECT output_json FROM agent_steps WHERE run_id=? AND agent_name='Writer Agent'",
                    (row["run_id"],),
                ).fetchall()
                if not any(json.loads(step["output_json"]).get("llm_used") is True for step in steps):
                    continue
                result.append({
                    "id": row["case_id"], "report_id": row["report_id"],
                    "source": json.loads(row["source_json"]),
                    "initial_draft": row["initial_draft"], "expected_text": row["expected_text"],
                })
                if limit is not None and len(result) >= limit:
                    break
        if not after:
            result.reverse()
        return result

    def list_writer_prompt_evaluations(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM writer_prompt_evaluations e WHERE e.rowid=(
                SELECT MAX(candidate.rowid) FROM writer_prompt_evaluations candidate
                WHERE candidate.candidate_version=e.candidate_version)
                ORDER BY e.rowid DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [_writer_prompt_evaluation(row) for row in rows]

    def start_writer_prompt_evaluation(
        self, candidate_version: int, baseline_version: int, benchmark_version: int,
        evidence: Dict[str, Any],
    ) -> str:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            benchmark = connection.execute(
                "SELECT version,holdout_used_at FROM evaluation_benchmarks ORDER BY version DESC LIMIT 1"
            ).fetchone()
            active = connection.execute(
                "SELECT version FROM writer_prompt_versions WHERE status='active'"
            ).fetchone()
            candidate = connection.execute(
                "SELECT version FROM writer_prompt_versions WHERE version=?", (candidate_version,)
            ).fetchone()
            if not candidate or not active or candidate_version == baseline_version or active["version"] != baseline_version:
                raise ValueError("候选 Writer 指令已是活动版本或版本不存在")
            if not benchmark or benchmark["version"] != benchmark_version or benchmark["holdout_used_at"]:
                raise ValueError("需要尚未使用 Holdout 的最新固定基准")
            if connection.execute(
                "SELECT 1 FROM writer_prompt_evaluations WHERE candidate_version=? AND benchmark_version=?",
                (candidate_version, benchmark_version),
            ).fetchone():
                raise ValueError("该指令版本已在当前基准上评测，不能重复运行")
            evaluation_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                """INSERT INTO writer_prompt_evaluations(
                id,candidate_version,baseline_version,benchmark_version,decision,reason,
                metrics_json,evidence_json,stage,progress_json,updated_at,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evaluation_id, candidate_version, baseline_version, benchmark_version,
                 "running", "评测进行中", json.dumps({"validation": None, "holdout": None}),
                 json.dumps(evidence, ensure_ascii=False), "validation",
                 json.dumps({"validation_cases": [], "holdout_cases": []}), now, now),
            )
        return evaluation_id

    def get_writer_prompt_evaluation(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM writer_prompt_evaluations WHERE id=?", (evaluation_id,),
            ).fetchone()
        return _writer_prompt_evaluation(row) if row else None

    def list_recoverable_writer_prompt_evaluations(
        self, stale_seconds: int, max_recoveries: int, max_calls: int, max_duration_ms: int,
    ) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM writer_prompt_evaluations WHERE stage IN ('validation','holdout_claimed')
                AND progress_json IS NOT NULL AND recovery_count<?
                AND (decision='error' OR decision='running' AND updated_at<?)
                ORDER BY created_at DESC""",
                (max_recoveries, cutoff),
            ).fetchall()
        return [_writer_prompt_evaluation(row) for row in rows
                if (row["model_calls"] < max_calls and row["duration_ms"] < max_duration_ms) or
                _writer_prompt_scoring_complete(row)]

    def claim_writer_prompt_evaluation(
        self, evaluation_id: str, stale_seconds: int, max_recoveries: int, max_calls: int,
        max_duration_ms: int,
    ) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM writer_prompt_evaluations WHERE id=?", (evaluation_id,),
            ).fetchone()
            if (row is None or row["stage"] not in {"validation", "holdout_claimed"} or
                    row["progress_json"] is None or row["recovery_count"] >= max_recoveries):
                return None
            scoring_complete = _writer_prompt_scoring_complete(row)
            if (row["model_calls"] >= max_calls or row["duration_ms"] >= max_duration_ms) and not scoring_complete:
                return None
            updated = datetime.fromisoformat(row["updated_at"] or row["created_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if row["decision"] == "running" and (now - updated).total_seconds() < stale_seconds:
                return None
            if row["decision"] not in {"running", "error"}:
                return None
            charged = min(20000, max(0, round((now - updated).total_seconds() * 1000))) if row["decision"] == "running" else 0
            duration_ms = row["duration_ms"] + charged
            if duration_ms >= max_duration_ms and not scoring_complete:
                connection.execute(
                    "UPDATE writer_prompt_evaluations SET stage='exhausted',decision='error',"
                    "reason=?,duration_ms=?,updated_at=? WHERE id=?",
                    ("Writer 指令评测累计时间预算已耗尽。", duration_ms, now.isoformat(), evaluation_id),
                )
                return None
            connection.execute(
                """UPDATE writer_prompt_evaluations SET decision='running',reason='评测进行中',
                recovery_count=recovery_count+1,duration_ms=?,updated_at=? WHERE id=?""",
                (duration_ms, now.isoformat(), evaluation_id),
            )
            claimed = connection.execute(
                "SELECT * FROM writer_prompt_evaluations WHERE id=?", (evaluation_id,),
            ).fetchone()
        return _writer_prompt_evaluation(claimed)

    def reserve_writer_prompt_call(
        self, evaluation_id: str, recovery_count: int, max_calls: int, max_duration_ms: int,
    ) -> None:
        with self._connection() as connection:
            changed = connection.execute(
                """UPDATE writer_prompt_evaluations SET model_calls=model_calls+1,updated_at=?
                WHERE id=? AND decision='running' AND recovery_count=? AND model_calls<?
                AND duration_ms<? AND stage IN ('validation','holdout_claimed')""",
                (utc_now(), evaluation_id, recovery_count, max_calls, max_duration_ms),
            )
            if changed.rowcount != 1:
                raise ValueError("Writer 指令评测调用或时间预算已耗尽，或运行已被接管")

    def save_writer_prompt_progress(
        self, evaluation_id: str, recovery_count: int, stage: str,
        progress: Dict[str, Any], duration_ms: int,
    ) -> None:
        with self._connection() as connection:
            changed = connection.execute(
                """UPDATE writer_prompt_evaluations SET progress_json=?,duration_ms=?,updated_at=?
                WHERE id=? AND decision='running' AND recovery_count=? AND stage=?""",
                (json.dumps(progress, ensure_ascii=False), duration_ms, utc_now(),
                 evaluation_id, recovery_count, stage),
            )
            if changed.rowcount != 1:
                raise ValueError("Writer 指令评测检查点已由其他运行接管")

    def interrupt_writer_prompt_evaluation(
        self, evaluation_id: str, recovery_count: int, reason: str,
        metrics: Dict[str, Any], evidence: Dict[str, Any], duration_ms: int,
        recoverable: bool,
    ) -> Dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """UPDATE writer_prompt_evaluations SET decision='error',reason=?,metrics_json=?,
                evidence_json=?,duration_ms=?,updated_at=?,stage=CASE WHEN ? THEN stage ELSE 'invalidated' END
                WHERE id=? AND decision='running' AND recovery_count=?""",
                (reason, json.dumps(metrics, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False),
                 duration_ms, utc_now(), int(recoverable), evaluation_id, recovery_count),
            )
        return self.get_writer_prompt_evaluation(evaluation_id)

    def finish_writer_prompt_evaluation(
        self, evaluation_id: str, decision: str, reason: str,
        metrics: Dict[str, Any], evidence: Dict[str, Any],
        recovery_count: int | None = None, duration_ms: int | None = None,
    ) -> Dict[str, Any]:
        if decision not in {"passed", "failed", "deferred", "error"}:
            raise ValueError("Writer 指令评测决策无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM writer_prompt_evaluations WHERE id=? AND decision='running'", (evaluation_id,)
            ).fetchone()
            if row is None or (recovery_count is not None and row["recovery_count"] != recovery_count):
                raise ValueError("Writer 指令评测不存在或已完成")
            active = connection.execute(
                "SELECT version FROM writer_prompt_versions WHERE status='active'"
            ).fetchone()
            if active is None or active["version"] != row["baseline_version"]:
                decision, reason = "stale", "评测期间活动 Writer 指令已切换，本次结果不能用于启用。"
            latest_benchmark = connection.execute(
                "SELECT version,fingerprint FROM evaluation_benchmarks ORDER BY version DESC LIMIT 1",
            ).fetchone()
            if (latest_benchmark is None or latest_benchmark["version"] != row["benchmark_version"] or
                    latest_benchmark["fingerprint"] != evidence.get("benchmark_fingerprint")):
                decision, reason = "stale", "评测期间固定基准已更新，本次结果不能用于启用。"
            if metrics.get("holdout") is not None:
                owner = connection.execute(
                    "SELECT holdout_run_id FROM evaluation_benchmarks WHERE version=?",
                    (row["benchmark_version"],),
                ).fetchone()
                if owner is None or owner["holdout_run_id"] != evaluation_id:
                    decision, reason = "stale", "Holdout 门禁不属于当前评测，本次结果不能用于启用。"
            elif decision == "passed":
                decision, reason = "stale", "缺少 Holdout 门禁结果，本次结果不能用于启用。"
            prompts = connection.execute(
                "SELECT version,instruction,sha256 FROM writer_prompt_versions WHERE version IN (?,?)",
                (row["baseline_version"], row["candidate_version"]),
            ).fetchall()
            prompt_by_version = {prompt["version"]: prompt for prompt in prompts}
            if any(
                version not in prompt_by_version or
                prompt_by_version[version]["sha256"] != evidence.get(f"{label}_sha256") or
                instruction_hash(prompt_by_version[version]["instruction"]) != prompt_by_version[version]["sha256"]
                for label, version in (("baseline", row["baseline_version"]),
                                       ("candidate", row["candidate_version"]))
            ):
                decision, reason = "stale", "评测期间 Writer 指令内容或指纹已变化，本次结果不能用于启用。"
            connection.execute(
                """UPDATE writer_prompt_evaluations SET decision=?,reason=?,metrics_json=?,evidence_json=?,
                completed_at=?,stage='completed',duration_ms=?,updated_at=? WHERE id=?""",
                (decision, reason, json.dumps(metrics, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False), utc_now(),
                 duration_ms if duration_ms is not None else row["duration_ms"], utc_now(), evaluation_id),
            )
            result = connection.execute("SELECT * FROM writer_prompt_evaluations WHERE id=?", (evaluation_id,)).fetchone()
        return _writer_prompt_evaluation(result)

    def get_writer_prompt(self, version: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM writer_prompt_versions WHERE version=?", (version,)).fetchone()
        return dict(row) if row else None

    def active_writer_prompt(self) -> Dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM writer_prompt_versions WHERE status='active' LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("没有活动 Writer 指令")
        return dict(row)

    def create_writer_prompt(self, addendum: str) -> Dict[str, Any]:
        addendum = validate_writer_addendum(addendum)
        instruction = writer_instruction(addendum)
        digest = instruction_hash(instruction)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM writer_prompt_versions WHERE sha256=?", (digest,)).fetchone():
                raise ValueError("相同的 Writer 指令版本已存在")
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM writer_prompt_versions").fetchone()[0]
            connection.execute(
                """INSERT INTO writer_prompt_versions(version,addendum,instruction,sha256,source,status,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (version, addendum, instruction, digest, "manual", "inactive", utc_now()),
            )
        return next(item for item in self.list_writer_prompt_versions() if item["version"] == version)

    def activate_writer_prompt(self, version: int) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT version,instruction,sha256 FROM writer_prompt_versions WHERE version=?", (version,)
            ).fetchone()
            if target is None:
                return False
            if instruction_hash(target["instruction"]) != target["sha256"]:
                raise ValueError("Writer 指令版本内容与指纹不一致")
            previous = connection.execute(
                "SELECT version FROM writer_prompt_versions WHERE status='active'"
            ).fetchone()
            if previous and previous["version"] == version:
                return True
            was_active = version == 1 or connection.execute(
                "SELECT 1 FROM writer_prompt_switches WHERE active_version=? LIMIT 1", (version,)
            ).fetchone() is not None
            if not was_active:
                latest = connection.execute(
                    """SELECT decision,baseline_version,benchmark_version FROM writer_prompt_evaluations
                    WHERE candidate_version=? ORDER BY rowid DESC LIMIT 1""", (version,)
                ).fetchone()
                newest_benchmark = connection.execute(
                    "SELECT MAX(version) FROM evaluation_benchmarks"
                ).fetchone()[0]
                if (not latest or latest["decision"] != "passed" or not previous or
                        latest["baseline_version"] != previous["version"] or
                        latest["benchmark_version"] != newest_benchmark):
                    raise ValueError("Writer 指令尚未通过相对当前版本的评测门禁")
            connection.execute("UPDATE writer_prompt_versions SET status='inactive' WHERE status='active'")
            connection.execute("UPDATE writer_prompt_versions SET status='active' WHERE version=?", (version,))
            if previous:
                connection.execute(
                    """INSERT INTO writer_prompt_switches(
                    id,previous_version,active_version,source,created_at) VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), previous["version"], version,
                     "manual" if was_active else "evaluation", utc_now()),
                )
        return True

    def record_writer_prompt_monitor_check(
        self, switch_id: str, active_version: int, previous_version: int,
        last_case_id: str, decision: str, reason: str, evidence: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if decision not in {"kept", "rolled_back"}:
            raise ValueError("Writer 指令监测决策无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            switch = connection.execute(
                "SELECT * FROM writer_prompt_switches ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            active = connection.execute(
                "SELECT version FROM writer_prompt_versions WHERE status='active'"
            ).fetchone()
            if (switch is None or switch["id"] != switch_id or switch["source"] != "evaluation" or
                    switch["active_version"] != active_version or
                    switch["previous_version"] != previous_version or
                    active is None or active["version"] != active_version):
                return None
            existing = connection.execute(
                "SELECT * FROM writer_prompt_monitor_checks WHERE switch_id=? AND last_case_id=?",
                (switch_id, last_case_id),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["evidence"] = json.loads(result.pop("evidence_json"))
                return result
            if decision == "rolled_back":
                previous = connection.execute(
                    "SELECT version FROM writer_prompt_versions WHERE version=?", (previous_version,)
                ).fetchone()
                if previous is None:
                    return None
                connection.execute(
                    "UPDATE writer_prompt_versions SET status='inactive' WHERE version=?", (active_version,)
                )
                connection.execute(
                    "UPDATE writer_prompt_versions SET status='active' WHERE version=?", (previous_version,)
                )
                connection.execute(
                    """INSERT INTO writer_prompt_switches(
                    id,previous_version,active_version,source,created_at) VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), active_version, previous_version, "auto_rollback", utc_now()),
                )
            result = {
                "id": str(uuid.uuid4()), "switch_id": switch_id, "last_case_id": last_case_id,
                "decision": decision, "reason": reason, "evidence": evidence, "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO writer_prompt_monitor_checks(
                id,switch_id,last_case_id,decision,reason,evidence_json,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (result["id"], switch_id, last_case_id, decision, reason,
                 json.dumps(evidence, ensure_ascii=False), result["created_at"]),
            )
        return result

    def active_strategy(self) -> Dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategies WHERE status='active' ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("没有活动报告策略")
        return self._strategy(row)

    def list_strategies(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM strategies ORDER BY version DESC").fetchall()
        return [self._strategy(row) for row in rows]

    def get_strategy(self, version: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM strategies WHERE version=?", (version,)).fetchone()
        return self._strategy(row) if row else None

    def latest_strategy_activation(self) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM strategy_activations ORDER BY rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def latest_strategy_monitor_check(self) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM strategy_monitor_checks ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def record_strategy_monitor_check(
        self, activation_id: str, active_version: int, previous_version: int,
        last_case_id: str, decision: str, reason: str, evidence: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if decision not in {"kept", "rolled_back"}:
            raise ValueError("监测决策无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            activation = connection.execute(
                "SELECT * FROM strategy_activations ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            active = connection.execute("SELECT version FROM strategies WHERE status='active'").fetchone()
            if (activation is None or activation["id"] != activation_id or activation["source"] != "evaluation"
                    or activation["active_version"] != active_version
                    or activation["previous_version"] != previous_version
                    or active is None or active["version"] != active_version):
                return None
            existing = connection.execute(
                "SELECT * FROM strategy_monitor_checks WHERE activation_id=? AND last_case_id=?",
                (activation_id, last_case_id),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["evidence"] = json.loads(result.pop("evidence_json"))
                return result
            if decision == "rolled_back":
                previous = connection.execute(
                    "SELECT version FROM strategies WHERE version=? AND status!='deferred'", (previous_version,)
                ).fetchone()
                if previous is None:
                    return None
                connection.execute("UPDATE strategies SET status='inactive' WHERE version=?", (active_version,))
                connection.execute("UPDATE strategies SET status='active' WHERE version=?", (previous_version,))
                connection.execute(
                    """INSERT INTO strategy_activations(id,previous_version,active_version,source,created_at)
                    VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), active_version, previous_version, "auto_rollback", utc_now()),
                )
            result = {
                "id": str(uuid.uuid4()), "activation_id": activation_id, "last_case_id": last_case_id,
                "decision": decision, "reason": reason, "evidence": evidence, "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO strategy_monitor_checks(id,activation_id,last_case_id,decision,reason,evidence_json,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (result["id"], activation_id, last_case_id, decision, reason,
                 json.dumps(evidence, ensure_ascii=False), result["created_at"]),
            )
        return result

    def save_strategy(self, config: Dict[str, Any], metrics: Dict[str, Any], status: str) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM strategies").fetchone()[0])
            connection.execute(
                "INSERT INTO strategies(version,config_json,metrics_json,status,created_at) VALUES(?,?,?,?,?)",
                (version, json.dumps(config, ensure_ascii=False), json.dumps(metrics, ensure_ascii=False), status, utc_now()),
            )
        return next(item for item in self.list_strategies() if item["version"] == version)

    def activate_strategy(self, version: int, source: str = "manual") -> bool:
        if source not in {"manual", "evaluation"}:
            raise ValueError("策略激活来源无效")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT version FROM strategies WHERE version=? AND status!='deferred'", (version,)
            ).fetchone()
            if row is None:
                return False
            previous = connection.execute("SELECT version FROM strategies WHERE status='active'").fetchone()
            if previous and previous["version"] == version:
                return True
            connection.execute("UPDATE strategies SET status='inactive' WHERE status='active'")
            connection.execute("UPDATE strategies SET status='active' WHERE version=?", (version,))
            if previous:
                connection.execute(
                    """INSERT INTO strategy_activations(id,previous_version,active_version,source,created_at)
                    VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), previous["version"], version, source, utc_now()),
                )
        return True

    def save_evaluation_run(
        self, candidate_version: int, decision: str, metrics: Dict[str, Any], reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO evaluation_runs(
                    id,candidate_version,decision,metrics_json,evidence_json,reason,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (run_id, candidate_version, decision, json.dumps(metrics, ensure_ascii=False),
                 json.dumps(evidence or {}, ensure_ascii=False), reason, utc_now()),
            )
        return run_id

    def list_evaluation_runs(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM evaluation_runs ORDER BY created_at DESC LIMIT 20").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    def get_evaluation_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM evaluation_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def start_agent_run(
        self, workflow: str, report_id: Optional[str] = None,
        instruction_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO agent_runs(id,workflow,report_id,status,duration_ms,created_at,updated_at,instruction_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, workflow, report_id, "running", 0, now, now,
                 json.dumps(instruction_snapshot or {}, ensure_ascii=False)),
            )
        return run_id

    def start_final_review(
        self, report_id: str, source_hash: str, final_hash: str, acknowledge_warnings: bool,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        handoff = {"source_sha256": source_hash, "final_sha256": final_hash,
                   "warnings_acknowledged": acknowledge_warnings}
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute(
                "SELECT status,origin,source_json FROM reports WHERE id=?", (report_id,),
            ).fetchone()
            if report is None or report["status"] != "draft" or report["origin"] != "generated":
                raise ValueError("只能审查未确认的系统草稿")
            if _source_sha256(report["source_json"]) != source_hash:
                raise ValueError("报告事实来源已变化，不能开始审查")
            critic_rule = self._active_critic_rule_in_connection(connection)
            connection.execute(
                """INSERT INTO agent_runs(id,workflow,report_id,status,duration_ms,created_at,updated_at,instruction_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, "final_review", report_id, "running", 0, now, now,
                 json.dumps({"critic_rules": {"version": critic_rule["version"],
                                              "fingerprint": critic_rule["fingerprint"]}}, ensure_ascii=False)),
            )
            connection.execute(
                """INSERT INTO agent_checkpoints(
                run_id,stage,attempt,candidate_draft,review_json,handoff_json,input_hash,llm_used,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_id, "prepared", 0, "", "{}", json.dumps(handoff, ensure_ascii=False),
                 final_hash, 0, now),
            )
        return run_id

    def complete_final_review(
        self, run_id: str, recovery_count: int, final_text: str, source_hash: str,
        duration_ms: int, gate_duration_ms: int, expected_decision: str,
        gate_execution: Dict[str, Any],
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT report_id FROM agent_runs WHERE id=? AND workflow='final_review'
                AND status='running' AND recovery_count=?""", (run_id, recovery_count),
            ).fetchone()
            checkpoint = connection.execute(
                "SELECT stage,handoff_json,review_json FROM agent_checkpoints WHERE run_id=?", (run_id,),
            ).fetchone()
            if run is None or checkpoint is None or checkpoint["stage"] != "critic_ready":
                raise ValueError("终稿审查已结束或已由其他恢复尝试接管")
            handoff = json.loads(checkpoint["handoff_json"])
            review = json.loads(checkpoint["review_json"])
            report = connection.execute(
                "SELECT status,origin,source_json FROM reports WHERE id=?", (run["report_id"],),
            ).fetchone()
            if (report is None or report["status"] != "draft" or report["origin"] != "generated" or
                    _source_sha256(report["source_json"]) != source_hash or
                    handoff["source_sha256"] != source_hash or
                    hashlib.sha256(final_text.encode("utf-8")).hexdigest() != handoff["final_sha256"]):
                raise ValueError("终稿或事实来源已变化，不能恢复审查")
            critic = connection.execute(
                """SELECT status,output_json,metrics_json FROM agent_steps
                WHERE run_id=? AND agent_name='Critic Agent'""", (run_id,),
            ).fetchall()
            if len(critic) != 1:
                raise ValueError("终稿审查的 Critic 检查点不完整")
            critic_output = json.loads(critic[0]["output_json"])
            critic_metrics = json.loads(critic[0]["metrics_json"])
            if (critic_output.get("unsafe") != review["unsafe"] or
                    critic_metrics.get("safety") != review["safety"]):
                raise ValueError("终稿审查检查点与 Critic 结果不一致")
            has_warnings = review["has_warnings"]
            critic_status = "passed" if not review["unsafe"] and not has_warnings else "needs_review"
            if critic[0]["status"] != critic_status:
                raise ValueError("终稿审查检查点与 Critic 状态不一致")
            decision = ("blocked" if review["unsafe"] else "passed"
                        if not has_warnings or handoff["warnings_acknowledged"] else "needs_review")
            if decision != expected_decision:
                raise ValueError("Gate 子进程决定与确定性门禁不一致")
            now = utc_now()
            connection.execute(
                """INSERT INTO agent_steps(
                id,run_id,agent_name,status,input_json,output_json,metrics_json,duration_ms,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), run_id, "Gate Agent", decision,
                 json.dumps({"critic_status": critic_status,
                             "warnings_acknowledged": handoff["warnings_acknowledged"],
                             "final_sha256": handoff["final_sha256"]}, ensure_ascii=False),
                 json.dumps({"decision": "阻止敏感信息入库" if decision == "blocked" else
                             "等待用户确认审查提示" if decision == "needs_review" else "允许确认终稿",
                             "execution": gate_execution},
                            ensure_ascii=False),
                 json.dumps({"safety": review["safety"]}, ensure_ascii=False), gate_duration_ms, now),
            )
            if decision == "passed":
                self._confirm_report_in_transaction(connection, run["report_id"], final_text)
            connection.execute(
                "UPDATE agent_checkpoints SET stage='completed',updated_at=? WHERE run_id=?", (now, run_id),
            )
            connection.execute(
                """UPDATE agent_runs SET status=?,duration_ms=?,completed_at=?,updated_at=?,error_code=NULL
                WHERE id=?""", (decision, duration_ms, now, now, run_id),
            )
            confirmed = self._report(connection.execute(
                "SELECT * FROM reports WHERE id=?", (run["report_id"],),
            ).fetchone()) if decision == "passed" else None
        return decision, confirmed

    def add_agent_step(
        self, run_id: str, agent_name: str, status: str, input_summary: Dict[str, Any],
        output_summary: Dict[str, Any], metrics: Dict[str, Any], duration_ms: int,
        checkpoint: Optional[Dict[str, Any]] = None,
        expected_recovery_count: Optional[int] = None,
        cumulative_duration_ms: Optional[int] = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            if expected_recovery_count is not None:
                owner = connection.execute(
                    "SELECT 1 FROM agent_runs WHERE id=? AND status='running' AND recovery_count=?",
                    (run_id, expected_recovery_count),
                ).fetchone()
                if owner is None:
                    raise ValueError("运行已由其他恢复尝试接管")
            step_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO agent_steps(
                    id,run_id,agent_name,status,input_json,output_json,metrics_json,duration_ms,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (step_id, run_id, agent_name, status, json.dumps(input_summary, ensure_ascii=False),
                 json.dumps(output_summary, ensure_ascii=False), json.dumps(metrics, ensure_ascii=False), duration_ms, utc_now()),
            )
            if checkpoint is not None:
                connection.execute(
                    """INSERT INTO agent_checkpoints(
                        run_id,stage,attempt,candidate_draft,review_json,handoff_json,input_hash,llm_used,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                        stage=excluded.stage,attempt=excluded.attempt,candidate_draft=excluded.candidate_draft,
                        review_json=excluded.review_json,handoff_json=excluded.handoff_json,input_hash=excluded.input_hash,
                        llm_used=excluded.llm_used,updated_at=excluded.updated_at""",
                    (run_id, checkpoint["stage"], checkpoint["attempt"], checkpoint["candidate_draft"],
                     json.dumps(checkpoint.get("review", {}), ensure_ascii=False),
                     json.dumps(checkpoint.get("handoff", {}), ensure_ascii=False), checkpoint["input_hash"],
                     int(checkpoint.get("llm_used", False)), utc_now()),
                )
                if "source_hash" in checkpoint:
                    connection.execute(
                        """INSERT INTO agent_step_snapshots(
                            step_id,run_id,stage,candidate_draft,review_json,handoff_json,
                            source_hash,replay_version,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (step_id, run_id, checkpoint["stage"], checkpoint["candidate_draft"],
                         json.dumps(checkpoint.get("review", {}), ensure_ascii=False),
                         json.dumps(checkpoint.get("handoff", {}), ensure_ascii=False),
                         checkpoint["source_hash"], checkpoint.get("replay_version", 1), utc_now()),
                    )
            if cumulative_duration_ms is None:
                connection.execute("UPDATE agent_runs SET updated_at=? WHERE id=?", (utc_now(), run_id))
            else:
                connection.execute(
                    "UPDATE agent_runs SET duration_ms=?,updated_at=? WHERE id=?",
                    (cumulative_duration_ms, utc_now(), run_id),
                )

    def update_agent_tokens(
        self, run_id: str, recovery_count: int, expected_used: int,
        used_delta: int, estimated_delta: int, limit: int | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            query = """UPDATE agent_runs SET token_used=token_used+?,
                token_estimated=token_estimated+?,updated_at=?
                WHERE id=? AND status='running' AND recovery_count=? AND token_used=?
                AND token_used+? >= 0 AND token_estimated+? >= 0"""
            params = (used_delta, estimated_delta, utc_now(), run_id, recovery_count,
                      expected_used, used_delta, estimated_delta)
            if limit is not None:
                query += " AND token_used+?<=?"
                params += (used_delta, limit)
            if connection.execute(query, params).rowcount != 1:
                raise ValueError("Token 预算不足或运行已由其他恢复尝试接管")

    def finish_agent_run(
        self, run_id: str, status: str, duration_ms: int, error_code: Optional[str] = None,
        expected_recovery_count: Optional[int] = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            if expected_recovery_count is None:
                connection.execute(
                    "UPDATE agent_runs SET status=?,duration_ms=?,completed_at=?,updated_at=?,error_code=? WHERE id=?",
                    (status, duration_ms, utc_now(), utc_now(), error_code, run_id),
                )
            else:
                connection.execute(
                    """UPDATE agent_runs SET status=?,duration_ms=?,completed_at=?,updated_at=?,error_code=?
                    WHERE id=? AND status='running' AND recovery_count=?""",
                    (status, duration_ms, utc_now(), utc_now(), error_code, run_id, expected_recovery_count),
                )

    def complete_daily_run(
        self, run_id: str, status: str, draft: str, baseline: str, duration_ms: int,
        expected_recovery_count: int,
    ) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            run = connection.execute(
                """SELECT report_id FROM agent_runs WHERE id=? AND workflow='daily_report'
                AND status='running' AND recovery_count=?""",
                (run_id, expected_recovery_count),
            ).fetchone()
            if run is None:
                raise ValueError("日报运行不存在或已结束")
            cursor = connection.execute(
                """UPDATE reports SET draft=?,initial_draft=?,updated_at=? WHERE id=?
                AND status='draft' AND origin='generated' AND draft=?""",
                (draft, draft, utc_now(), run["report_id"], baseline),
            )
            if cursor.rowcount != 1:
                raise ValueError("草稿已被修改，不能覆盖用户编辑")
            connection.execute(
                "UPDATE agent_runs SET status=?,duration_ms=?,completed_at=?,updated_at=?,error_code=NULL WHERE id=?",
                (status, duration_ms, utc_now(), utc_now(), run_id),
            )
            row = connection.execute("SELECT * FROM reports WHERE id=?", (run["report_id"],)).fetchone()
        return self._report(row)

    def get_agent_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["instruction_snapshot"] = json.loads(item.pop("instruction_json"))
            steps = connection.execute(
                "SELECT * FROM agent_steps WHERE run_id=? ORDER BY created_at,id", (run_id,)
            ).fetchall()
            item["steps"] = [self._agent_step(step) for step in steps]
        return item

    def get_agent_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agent_checkpoints WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["review"] = json.loads(item.pop("review_json"))
        item["handoff"] = json.loads(item.pop("handoff_json"))
        item["llm_used"] = bool(item["llm_used"])
        return item

    def list_agent_step_snapshots(self, run_id: str) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_step_snapshots WHERE run_id=? ORDER BY created_at,step_id", (run_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["review"] = json.loads(item.pop("review_json"))
            item["handoff"] = json.loads(item.pop("handoff_json"))
            result.append(item)
        return result

    def list_recoverable_agent_runs(
        self, stale_seconds: int = 60, max_recoveries: int = 2, workflow: str = "daily_report",
    ) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id,workflow,report_id,status,recovery_count,error_code,duration_ms,
                token_used,token_estimated,updated_at FROM agent_runs
                WHERE workflow=? AND recovery_count<? AND
                (status='failed' OR (status='running' AND updated_at<?))
                ORDER BY updated_at DESC""",
                (workflow, max_recoveries, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_agent_run(
        self, run_id: str, stale_seconds: int = 60, max_recoveries: int = 2,
        stale_charge_ms: int = 0, workflow: str = "daily_report",
    ) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE agent_runs SET status='running',recovery_count=recovery_count+1,
                duration_ms=duration_ms+CASE WHEN status='running' THEN ? ELSE 0 END,
                completed_at=NULL,error_code=NULL,updated_at=? WHERE id=? AND workflow=?
                AND recovery_count<? AND (status='failed' OR (status='running' AND updated_at<?))""",
                (max(0, stale_charge_ms), utc_now(), run_id, workflow, max_recoveries, cutoff),
            )
        return cursor.rowcount == 1

    def start_strategy_evaluation(self, snapshot: Dict[str, Any], evidence: Dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute("SELECT version FROM strategies WHERE status='active'").fetchone()
            if active is None or active["version"] != snapshot["baseline_version"]:
                raise ValueError("活动策略已变化，请重新提案")
            if snapshot["benchmark_version"] is not None:
                benchmark = connection.execute(
                    "SELECT fingerprint FROM evaluation_benchmarks WHERE version=?",
                    (snapshot["benchmark_version"],),
                ).fetchone()
                if benchmark is None or benchmark["fingerprint"] != snapshot["benchmark_fingerprint"]:
                    raise ValueError("固定基准已变化，请重新提案")
            connection.execute(
                """INSERT INTO agent_runs(id,workflow,report_id,status,duration_ms,created_at,updated_at,instruction_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, "strategy_evaluation", None, "running", 0, now, now,
                 json.dumps({"evaluation_rules": {"version": 1}}, ensure_ascii=False)),
            )
            connection.execute(
                """INSERT INTO strategy_evaluation_checkpoints(
                    run_id,stage,snapshot_json,metrics_json,evidence_json,updated_at
                ) VALUES(?,?,?,?,?,?)""",
                (run_id, "prepared", json.dumps(snapshot, ensure_ascii=False),
                 json.dumps({"current_validation": None, "candidate_validation": None,
                             "current_holdout": None, "candidate_holdout": None}),
                 json.dumps(evidence, ensure_ascii=False), now),
            )
        return run_id

    def get_strategy_evaluation_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_evaluation_checkpoints WHERE run_id=?", (run_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["snapshot"] = json.loads(item.pop("snapshot_json"))
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def update_strategy_evaluation_checkpoint(
        self, run_id: str, recovery_count: int, expected_stage: str, next_stage: str,
        metrics: Dict[str, Any], evidence: Dict[str, Any], duration_ms: int,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT 1 FROM agent_runs WHERE id=? AND workflow='strategy_evaluation' "
                "AND status='running' AND recovery_count=?", (run_id, recovery_count),
            ).fetchone()
            if owner is None:
                raise ValueError("评测运行已由其他恢复尝试接管")
            changed = connection.execute(
                """UPDATE strategy_evaluation_checkpoints SET stage=?,metrics_json=?,evidence_json=?,updated_at=?
                WHERE run_id=? AND stage=?""",
                (next_stage, json.dumps(metrics, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False),
                 utc_now(), run_id, expected_stage),
            )
            if changed.rowcount != 1:
                raise ValueError("评测检查点阶段已变化")
            connection.execute("UPDATE agent_runs SET duration_ms=?,updated_at=? WHERE id=?",
                               (duration_ms, utc_now(), run_id))

    def claim_strategy_evaluation_holdout(self, run_id: str, recovery_count: int) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT c.stage,c.snapshot_json FROM strategy_evaluation_checkpoints c
                JOIN agent_runs r ON r.id=c.run_id WHERE c.run_id=? AND c.stage='validation_ready'
                AND r.status='running' AND r.recovery_count=?""", (run_id, recovery_count),
            ).fetchone()
            if row is None:
                raise ValueError("评测运行状态已变化")
            snapshot = json.loads(row["snapshot_json"])
            benchmark = connection.execute(
                "SELECT version,fingerprint,holdout_used_at FROM evaluation_benchmarks "
                "ORDER BY version DESC LIMIT 1",
            ).fetchone()
            if (benchmark is None or benchmark["version"] != snapshot["benchmark_version"] or
                    benchmark["fingerprint"] != snapshot["benchmark_fingerprint"] or
                    benchmark["holdout_used_at"] is not None):
                return False
            connection.execute(
                "UPDATE evaluation_benchmarks SET holdout_used_at=?,holdout_run_id=? WHERE version=?",
                (utc_now(), run_id, benchmark["version"]),
            )
            connection.execute(
                "UPDATE strategy_evaluation_checkpoints SET stage='holdout_claimed',updated_at=? WHERE run_id=?",
                (utc_now(), run_id),
            )
            connection.execute("UPDATE agent_runs SET updated_at=? WHERE id=?", (utc_now(), run_id))
        return True

    def finalize_strategy_evaluation(
        self, run_id: str, recovery_count: int, decision: str, reason: str, duration_ms: int,
    ) -> Dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id=? AND workflow='strategy_evaluation' "
                "AND status='running' AND recovery_count=?", (run_id, recovery_count),
            ).fetchone()
            checkpoint = connection.execute(
                "SELECT * FROM strategy_evaluation_checkpoints WHERE run_id=?", (run_id,),
            ).fetchone()
            if run is None or checkpoint is None or checkpoint["stage"] != "decision_ready":
                raise ValueError("评测运行不能提交或已由其他恢复尝试接管")
            snapshot = json.loads(checkpoint["snapshot_json"])
            metrics = json.loads(checkpoint["metrics_json"])
            evidence = json.loads(checkpoint["evidence_json"])
            active = connection.execute("SELECT version FROM strategies WHERE status='active'").fetchone()
            if active is None or active["version"] != snapshot["baseline_version"]:
                raise ValueError("活动策略已变化，不能提交旧评测")
            benchmark = None
            if snapshot["benchmark_version"] is not None:
                benchmark = connection.execute(
                    "SELECT * FROM evaluation_benchmarks WHERE version=?",
                    (snapshot["benchmark_version"],),
                ).fetchone()
                if benchmark is None or benchmark["fingerprint"] != snapshot["benchmark_fingerprint"]:
                    raise ValueError("固定基准已变化，不能提交旧评测")
            holdout_evaluated = evidence.get("holdout_evaluated") is True
            if holdout_evaluated and (benchmark is None or benchmark["holdout_run_id"] != run_id or
                                       metrics["current_holdout"] is None or metrics["candidate_holdout"] is None):
                raise ValueError("Holdout 门禁归属或指标不一致")
            if not holdout_evaluated and (metrics["current_holdout"] is not None or
                                          metrics["candidate_holdout"] is not None):
                raise ValueError("Holdout 指标与检查点不一致")
            improves = metrics["candidate_validation"]["score"] > metrics["current_validation"]["score"]
            validation_passed = (benchmark is not None and snapshot["validation_count"] >= 3 and improves and
                                 snapshot["candidate_config"] != snapshot["baseline_config"])
            if (not holdout_evaluated and validation_passed and benchmark is not None and
                    benchmark["holdout_used_at"] is None and
                    benchmark["version"] == connection.execute(
                        "SELECT MAX(version) FROM evaluation_benchmarks",
                    ).fetchone()[0]):
                raise ValueError("尚未领取可用的 Holdout 门禁")
            safe = bool(holdout_evaluated and
                        metrics["candidate_holdout"]["score"] >= metrics["current_holdout"]["score"] and
                        metrics["candidate_holdout"]["safety"] >= metrics["current_holdout"]["safety"])
            expected_decision = "activated" if validation_passed and safe else "deferred"
            if decision != expected_decision or evidence.get("holdout_evaluated") != holdout_evaluated:
                raise ValueError("评测门禁结论与检查点不一致")
            now = utc_now()
            candidate_version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM strategies",
            ).fetchone()[0])
            connection.execute(
                "INSERT INTO strategies(version,config_json,metrics_json,status,created_at) VALUES(?,?,?,?,?)",
                (candidate_version, json.dumps(snapshot["candidate_config"], ensure_ascii=False),
                 json.dumps(metrics, ensure_ascii=False),
                 "activated" if decision == "activated" else "deferred", now),
            )
            if decision == "activated":
                connection.execute("UPDATE strategies SET status='inactive' WHERE version=?",
                                   (snapshot["baseline_version"],))
                connection.execute("UPDATE strategies SET status='active' WHERE version=?", (candidate_version,))
                connection.execute(
                    """INSERT INTO strategy_activations(id,previous_version,active_version,source,created_at)
                    VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), snapshot["baseline_version"], candidate_version, "evaluation", now),
                )
            evaluation_run_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO evaluation_runs(id,candidate_version,decision,metrics_json,evidence_json,reason,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (evaluation_run_id, candidate_version, decision, json.dumps(metrics, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False), reason, now),
            )
            if decision == "activated":
                for rule in evidence.get("rules", []):
                    value = str(rule["after"]).lower() if isinstance(rule["after"], bool) else str(rule["after"])
                    connection.execute(
                        """INSERT INTO memory_entries(
                            id,scope,project_id,memory_key,value,source_type,source_id,supersedes_id,
                            status,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), "personal", None, rule["key"], value, "evaluation",
                         evaluation_run_id, None, "active", now, now),
                    )
            step_data = (
                ("Fact Agent", "passed",
                 {"validation_count": snapshot["validation_count"], "holdout_count": snapshot["holdout_count"]},
                 {"dataset_ready": snapshot["validation_count"] >= 3 and snapshot["holdout_count"] >= 1}, {}),
                ("Writer Agent", "passed", {"role": "生成候选写作策略"},
                 {"candidate_config": snapshot["candidate_config"]}, {}),
                ("Critic Agent", "passed", {"role": "回放评测候选策略"},
                 {"candidate_validation": metrics["candidate_validation"],
                  "candidate_holdout": metrics["candidate_holdout"]}, {}),
                ("Gate Agent", decision, {"role": "检查 Validation 提升与 Holdout 非退化"},
                 {"reason": reason},
                 {"current_validation": metrics["current_validation"]["score"],
                  "candidate_validation": metrics["candidate_validation"]["score"],
                  "current_holdout": metrics["current_holdout"]["score"] if metrics["current_holdout"] else None,
                  "candidate_holdout": metrics["candidate_holdout"]["score"] if metrics["candidate_holdout"] else None}),
            )
            for agent_name, status, input_summary, output_summary, step_metrics in step_data:
                connection.execute(
                    """INSERT INTO agent_steps(
                        id,run_id,agent_name,status,input_json,output_json,metrics_json,duration_ms,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), run_id, agent_name, status,
                     json.dumps(input_summary, ensure_ascii=False),
                     json.dumps(output_summary, ensure_ascii=False),
                     json.dumps(step_metrics, ensure_ascii=False), 0, utc_now()),
                )
            instruction = {
                "evaluation_rules": {"version": 1},
                "evaluation": {"run_id": evaluation_run_id, "candidate_version": candidate_version,
                               "baseline_version": snapshot["baseline_version"],
                               "benchmark_version": snapshot["benchmark_version"],
                               "benchmark_fingerprint": snapshot["benchmark_fingerprint"]},
            }
            connection.execute(
                """UPDATE agent_runs SET status=?,duration_ms=?,completed_at=?,updated_at=?,
                error_code=NULL,instruction_json=? WHERE id=?""",
                (decision, duration_ms, now, now, json.dumps(instruction, ensure_ascii=False), run_id),
            )
            connection.execute(
                "UPDATE strategy_evaluation_checkpoints SET stage='completed',updated_at=? WHERE run_id=?",
                (now, run_id),
            )
        return {"candidate": self.get_strategy(candidate_version), "evaluation_run_id": evaluation_run_id,
                "trace_run_id": run_id}

    def list_agent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            runs = connection.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for run in runs:
                item = dict(run)
                item["instruction_snapshot"] = json.loads(item.pop("instruction_json"))
                steps = connection.execute(
                    "SELECT * FROM agent_steps WHERE run_id=? ORDER BY created_at", (item["id"],)
                ).fetchall()
                item["steps"] = [self._agent_step(step) for step in steps]
                result.append(item)
        return result

    def backup_to(self, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as source:
            target = sqlite3.connect(str(target_path))
            try:
                source.backup(target)
            finally:
                target.close()

    @staticmethod
    def inspect_backup(path: Path) -> Dict[str, int]:
        required = {
            "projects": {"id", "name", "path", "created_at"},
            "reports": {"id", "kind", "report_date", "source_json", "draft", "final_text", "status", "strategy_version"},
            "evaluation_cases": {"id", "report_id", "split", "source_json", "expected_text"},
            "strategies": {"version", "config_json", "metrics_json", "status"},
            "evaluation_runs": {"id", "candidate_version", "decision", "metrics_json", "reason"},
        }
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("备份文件校验失败")
                if source.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("备份文件存在关联数据错误")
                for table, columns in required.items():
                    actual = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
                    if not columns.issubset(actual):
                        raise ValueError("这不是兼容的 EvoReport 数据库备份")
                if source.execute("SELECT 1 FROM strategies WHERE status='active' LIMIT 1").fetchone() is None:
                    raise ValueError("备份文件缺少当前写作策略")
                return {
                    "projects": source.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                    "reports": source.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
                    "confirmed_reports": source.execute("SELECT COUNT(*) FROM reports WHERE status='confirmed'").fetchone()[0],
                    "work_notes": source.execute("SELECT COUNT(*) FROM work_notes").fetchone()[0]
                    if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_notes'").fetchone() else 0,
                }
        except sqlite3.DatabaseError as exc:
            raise ValueError("文件不是有效的 SQLite 数据库备份") from exc

    def restore_from(self, source_path: Path, safety_backup_path: Path) -> Dict[str, int]:
        with self._lock:
            preview = self.inspect_backup(source_path)
            self.backup_to(safety_backup_path)
            try:
                with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    destination = self._connect()
                    try:
                        source.backup(destination)
                    finally:
                        destination.close()
                self._init_schema()
            except Exception:
                with closing(sqlite3.connect(safety_backup_path.resolve().as_uri() + "?mode=ro", uri=True)) as safety_source:
                    destination = self._connect()
                    try:
                        safety_source.backup(destination)
                    finally:
                        destination.close()
                raise
            return preview

    @staticmethod
    def _report(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["source"] = json.loads(item.pop("source_json"))
        return item

    @staticmethod
    def _strategy(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        item["metrics"] = json.loads(item.pop("metrics_json"))
        return item

    @staticmethod
    def _agent_step(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json"))
        item["output"] = json.loads(item.pop("output_json"))
        item["metrics"] = json.loads(item.pop("metrics_json"))
        return item


def _benchmark(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["validation_ids"] = json.loads(item.pop("validation_ids_json"))
    item["holdout_ids"] = json.loads(item.pop("holdout_ids_json"))
    coverage_json = item.pop("coverage_json")
    item["coverage"] = json.loads(coverage_json) if coverage_json else None
    scenarios_json = item.pop("scenarios_json", None)
    scenarios = json.loads(scenarios_json) if scenarios_json else None
    item["scenario_version"] = 1 if scenarios else None
    item["prompt_eligible_counts"] = ({split: sum("eligible" in tags for tags in cases.values())
                                        for split, cases in scenarios.items()} if scenarios else None)
    return item


def _writer_prompt_evaluation(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["metrics"] = json.loads(item.pop("metrics_json"))
    item["evidence"] = json.loads(item.pop("evidence_json"))
    progress_json = item.pop("progress_json", None)
    item["progress"] = json.loads(progress_json) if progress_json else None
    return item


def _validated_memory_expiry(expires_on: Optional[str]) -> Optional[str]:
    if expires_on is None:
        return None
    try:
        expiry = date.fromisoformat(expires_on)
    except (TypeError, ValueError) as exc:
        raise ValueError("项目备注到期日期无效") from exc
    if expiry < _memory_today():
        raise ValueError("项目备注到期日期不能早于今天")
    return expiry.isoformat()


def _memory_entry(row: sqlite3.Row, today: Optional[str] = None) -> Dict[str, Any]:
    item = dict(row)
    current_day = today or _memory_today().isoformat()
    item["is_expired"] = bool(
        item["scope"] == "project" and item["expires_on"] is not None and
        item["expires_on"] < current_day
    )
    return item


def _memory_reference(item: Dict[str, Any], candidate_value: str) -> Dict[str, Any]:
    return {
        "id": item["id"], "value": item["value"], "status": item["status"],
        "is_expired": item["is_expired"],
        "similarity": round(memory_similarity(item["value"], candidate_value), 4),
    }


def _project_memory_guidance(notes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    focus: Dict[str, Dict[str, Any]] = {}
    preference_votes: Dict[str, List[tuple[Any, str]]] = {}
    for note in notes:
        text = str(note.get("text") or "")
        project_id, note_id = str(note.get("project_id") or ""), str(note.get("id") or "")
        sections = []
        if re.search(r"风险|问题|阻塞|依赖|兼容", text):
            sections.append("遇到的问题")
        if re.search(r"计划|后续|长期|目标|里程碑", text):
            sections.append("明日计划")
        if re.search(r"成果|交付|进展", text):
            sections.append("今日完成")
        if sections and project_id and note_id:
            item = focus.setdefault(project_id, {"project_id": project_id, "sections": [], "source_ids": []})
            item["sections"] = list(dict.fromkeys(item["sections"] + sections))
            item["source_ids"].append(note_id)
        preferences: Dict[str, Any] = {}
        if re.search(r"项目符号|无编号", text):
            preferences["numbered"] = False
        elif re.search(r"编号|序号", text):
            preferences["numbered"] = True
        if re.search(r"不(?:展示|需要|要).{0,3}统计|无需.{0,3}统计", text):
            preferences["include_statistics"] = False
        elif re.search(r"统计|数据概览", text):
            preferences["include_statistics"] = True
        if re.search(r"简洁|精简|简短", text):
            preferences["max_item_length"] = 70
        elif re.search(r"详细|展开", text):
            preferences["max_item_length"] = 130
        for key, value in preferences.items():
            preference_votes.setdefault(key, []).append((value, note_id))
    expression_preferences: Dict[str, Any] = {}
    expression_sources: Dict[str, List[str]] = {}
    for key, votes in preference_votes.items():
        values = {value for value, _ in votes}
        if len(values) == 1:
            expression_preferences[key] = votes[0][0]
            expression_sources[key] = [source_id for _, source_id in votes]
    return {"section_focus": list(focus.values()),
            "expression_preferences": expression_preferences,
            "expression_sources": expression_sources}


def _writer_prompt_scoring_complete(row: sqlite3.Row) -> bool:
    if row["stage"] != "holdout_claimed" or not row["progress_json"]:
        return False
    progress = json.loads(row["progress_json"])
    evidence = json.loads(row["evidence_json"])
    return all(
        len(progress.get(key, [])) == count and
        all("baseline" in item and "candidate" in item for item in progress[key])
        for key, count in (("validation_cases", evidence.get("validation_count", 3)),
                           ("holdout_cases", evidence.get("holdout_count", 1)))
    )


def _benchmark_case(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["source"] = json.loads(item.pop("source_json"))
    return item


def _case_fingerprint(cases: List[Dict[str, Any]]) -> str:
    stable = [
        {"id": item["id"], "report_id": item["report_id"], "split": item["split"],
         "source": item["source"], "expected_text": item["expected_text"]}
        for item in sorted(cases, key=lambda case: case["id"])
    ]
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prompt_input_snapshot(
    connection: sqlite3.Connection, cases: List[Dict[str, Any]],
) -> tuple[str, Dict[str, sqlite3.Row]]:
    if not cases:
        return hashlib.sha256(b"[]").hexdigest(), {}
    report_ids = tuple({item["report_id"] for item in cases})
    placeholders = ",".join("?" for _ in report_ids)
    rows = connection.execute(
        f"SELECT id,kind,origin,initial_draft FROM reports WHERE id IN ({placeholders})",
        report_ids,
    ).fetchall()
    reports = {row["id"]: row for row in rows}
    stable = []
    for case in sorted(cases, key=lambda item: item["id"]):
        report = reports.get(case["report_id"])
        draft = (report["initial_draft"] if report and report["kind"] == "daily"
                 and report["origin"] == "generated" and report["initial_draft"] else None)
        stable.append({
            "case_id": case["id"], "report_id": case["report_id"],
            "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest() if draft else None,
        })
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), reports


def _benchmark_fingerprint(
    validation_hash: str, holdout_hash: str, validation_draft_hash: str, holdout_draft_hash: str,
    scenarios_json: Optional[str] = None,
) -> str:
    return hashlib.sha256((
        validation_hash + holdout_hash + validation_draft_hash + holdout_draft_hash + (scenarios_json or "")
    ).encode("utf-8")).hexdigest()


def _case_scenarios(connection: sqlite3.Connection, cases: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    _, reports = _prompt_input_snapshot(connection, cases)
    result = {}
    for case in cases:
        coverage = _validation_coverage(connection, [case])
        tags = [f"{dimension}:{key}" for dimension in ("origin", "source_types", "project_count", "length", "edits")
                for key, count in coverage[dimension].items() if count]
        report = reports.get(case["report_id"])
        if report and report["kind"] == "daily" and report["origin"] == "generated" and report["initial_draft"]:
            tags.append("eligible")
        result[case["id"]] = tags
    return result


def _stratified_cases(cases: List[Dict[str, Any]], tags: Dict[str, List[str]], limit: int) -> List[Dict[str, Any]]:
    remaining = sorted(cases, key=lambda item: item["id"])
    counts: Dict[str, int] = {}
    for case in remaining:
        for tag in set(tags.get(case["id"], [])) - {"eligible"}:
            counts[tag] = counts.get(tag, 0) + 1
    covered: set[str] = set()
    selected = []
    while remaining and len(selected) < limit:
        def priority(case: Dict[str, Any]) -> tuple[float, str]:
            novel = set(tags.get(case["id"], [])) - covered - {"eligible"}
            return (-sum(1 / counts[tag] for tag in novel), case["id"])
        chosen = min(remaining, key=priority)
        selected.append(chosen)
        covered.update(tags.get(chosen["id"], []))
        remaining.remove(chosen)
    return selected


def _validation_coverage(connection: sqlite3.Connection, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    coverage: Dict[str, Any] = {
        "version": 1, "sample_count": 0,
        "origin": {"generated": 0, "historical": 0},
        "source_types": {"manual": 0, "git": 0, "mixed": 0, "unattributed": 0},
        "project_count": {"zero": 0, "one": 0, "multiple": 0},
        "length": {"short": 0, "medium": 0, "long": 0},
        "edits": {"changed": 0, "unchanged": 0, "unknown": 0},
        "warnings": [],
    }
    if not cases:
        coverage["warnings"].append("尚无可分析的 Validation 日报")
        return coverage
    report_ids = tuple({item["report_id"] for item in cases})
    placeholders = ",".join("?" for _ in report_ids)
    rows = connection.execute(
        f"SELECT id,kind,origin,initial_draft FROM reports WHERE id IN ({placeholders})",
        report_ids,
    ).fetchall()
    reports = {row["id"]: row for row in rows}
    for case in cases:
        report = reports.get(case["report_id"])
        if not report or report["kind"] != "daily" or report["origin"] not in {"generated", "historical"}:
            continue
        source = case["source"] if isinstance(case["source"], dict) else {}
        coverage["sample_count"] += 1
        coverage["origin"][report["origin"]] += 1
        fact_sources = source.get("fact_sources", [])
        types = {item.get("source_type") for item in fact_sources if isinstance(item, dict)}
        if not types:
            if str(source.get("manual_completed", "")).strip():
                types.add("manual")
            projects = source.get("projects", [])
            if isinstance(projects, list) and any(
                isinstance(project, dict) and isinstance(project.get("activity"), dict)
                and project["activity"].get("commits") for project in projects
            ):
                types.add("git_commit")
        category = ("mixed" if {"manual", "git_commit"} <= types else
                    "manual" if "manual" in types else "git" if "git_commit" in types else "unattributed")
        coverage["source_types"][category] += 1
        projects = source.get("projects", [])
        count = len(projects) if isinstance(projects, list) else 0
        coverage["project_count"]["zero" if count == 0 else "one" if count == 1 else "multiple"] += 1
        length = len(str(case.get("expected_text") or "").strip())
        coverage["length"]["short" if length < 200 else "medium" if length < 600 else "long"] += 1
        if report["origin"] == "generated" and report["initial_draft"] is not None:
            changed = report["initial_draft"].strip() != str(case.get("expected_text") or "").strip()
            coverage["edits"]["changed" if changed else "unchanged"] += 1
        else:
            coverage["edits"]["unknown"] += 1
    if coverage["sample_count"] < 3:
        coverage["warnings"].append("Validation 真实日报少于 3 份")
    if coverage["sample_count"]:
        for key, label in (("source_types", "事实来源"), ("project_count", "项目数量"), ("length", "终稿篇幅")):
            if sum(value > 0 for value in coverage[key].values()) == 1:
                coverage["warnings"].append(f"{label}只覆盖一种场景")
        if coverage["origin"]["generated"] < 3:
            coverage["warnings"].append("系统生成日报少于 3 份，无法完成 Writer 指令评测")
        if coverage["edits"]["changed"] == 0:
            coverage["warnings"].append("尚无修改初稿的终稿样本")
    return coverage
