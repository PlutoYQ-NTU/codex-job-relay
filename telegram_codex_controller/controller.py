from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None  # type: ignore[assignment]


APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
ENV_PATH = APP_DIR / ".env"
REPORT_INSTRUCTIONS = """请完成用户任务。任务结束时必须输出一份结构化 Markdown 报告，包含：

1. Task Summary
2. Actions Taken
3. Files Read
4. Files Modified
5. Commands Run
6. Results
7. Errors or Warnings
8. Suggested Next Steps

用户原始任务如下：
{user_prompt}
"""


REMOTE_EXECUTION_CONSTRAINTS = """Remote execution constraints

- Do not enter or recursively scan these directories:
  - .venv/
  - .codex/
  - .codex_deps/
  - .pip-cache/
  - .tmp/
  - .tmp_matt_skills/
  - .scratch/
  - artifacts/
  - data/hf_cache/
  - data/processed/
  - data/tokenized/
  - outputs/
- Do not use unfiltered `Get-ChildItem -Recurse`.
- Do not use unfiltered `rg --files` to scan the whole project.
- If you need to inspect project structure, prefer:
  - `git status --short`
  - `git ls-files`
  - `Get-ChildItem -Force` at the project root only
- Do not read large files, saved artifacts, tokenized data, processed data, or
  historical output directories unless the user explicitly requests it.
- By default, finish the task with a structured Markdown report.
"""


WRITE_REPORT_INSTRUCTIONS = """Approved write or long-running task report requirements

For this approved write or long-running task, the final Markdown report must include:

1. Files modified
2. Commands run
3. Git diff summary
4. Suggested next steps
"""

TASK_EXECUTION_INSTRUCTIONS = """Remote agent execution requirements

- Current risk level: {risk_level}
- If the user task requires running a project script, and that operation is
  allowed by the current risk level and sandbox, run it directly. Do not only
  give the user the command.
- Do not leave critical task steps for the user to run manually.
- Only list commands as "requires manual execution" when:
  - permission is insufficient
  - dependencies are missing
  - required data or artifacts are missing
  - the command would cross a safety boundary
  - the task is dangerous
  - the user explicitly asked to generate commands without running them
- If permission prevents writing to a target directory, the report must include:
  - the command attempted
  - the exact error
  - the directory that could not be written
  - whether a temporary-directory fallback was used
  - the temporary-directory path
- For run_expensive tasks that the user approved, training or evaluation scripts
  may be run directly.
- For read_only tasks, do not start training. Only inspect, analyze, and report.
- For write_workspace tasks, code or configuration may be changed, but long
  training should not be started unless the user explicitly requested it.
"""

READ_ONLY_KEYWORDS = (
    "检查",
    "读取",
    "总结",
    "报告",
    "分析",
    "review",
    "status",
    "inspect",
    "summarize",
    "不要修改",
    "只读",
)
WRITE_WORKSPACE_KEYWORDS = (
    "修改",
    "修复",
    "新增",
    "创建",
    "更新",
    "重构",
    "edit",
    "fix",
    "modify",
    "create",
    "update",
    "refactor",
    "write",
)
RUN_EXPENSIVE_KEYWORDS = (
    "训练",
    "train",
    "evaluate all",
    "run full",
    "long run",
)
DANGEROUS_KEYWORDS = (
    "删除全部",
    "清空",
    "格式化",
    "rm -rf",
    "del /s",
    "remove-item -recurse",
    "git reset --hard",
    "git clean -fdx",
    "danger-full-access",
    "读取 .env",
    "显示 token",
    "上传密钥",
    "系统目录",
)
APPROVAL_RISK_LEVELS = {"write_workspace", "run_expensive"}
LOCAL_TRAIN_PROJECT_KEY = "main"
LOCAL_TRAIN_PYTHON_RELATIVE = Path(".venv") / "Scripts" / "python.exe"
LOCAL_TRAIN_SCRIPT_RELATIVE = Path("scripts") / "train.py"
LOCAL_TRAIN_CONFIG_ALIASES = {
    "default": Path("configs") / "local_train.yaml",
    "resume": Path("configs") / "local_train_resume.yaml",
    "experiment": Path("configs") / "local_train_experiment.yaml",
}


def find_keyword_matches(text: str, keywords: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def classify_prompt_risk(prompt: str) -> dict[str, Any]:
    dangerous_matches = find_keyword_matches(prompt, DANGEROUS_KEYWORDS)
    if dangerous_matches:
        return {
            "level": "dangerous",
            "reason": f"Matched dangerous keyword: {dangerous_matches[0]}",
            "matches": dangerous_matches,
        }

    expensive_matches = find_keyword_matches(prompt, RUN_EXPENSIVE_KEYWORDS)
    if expensive_matches:
        return {
            "level": "run_expensive",
            "reason": f"Matched long-running keyword: {expensive_matches[0]}",
            "matches": expensive_matches,
        }

    write_scan_text = prompt
    for read_only_phrase in ("不要修改", "只读"):
        write_scan_text = write_scan_text.replace(read_only_phrase, "")
    write_matches = find_keyword_matches(write_scan_text, WRITE_WORKSPACE_KEYWORDS)
    if write_matches:
        return {
            "level": "write_workspace",
            "reason": f"Matched workspace-write keyword: {write_matches[0]}",
            "matches": write_matches,
        }

    read_matches = find_keyword_matches(prompt, READ_ONLY_KEYWORDS)
    if read_matches:
        return {
            "level": "read_only",
            "reason": f"Matched read-only keyword: {read_matches[0]}",
            "matches": read_matches,
        }

    return {
        "level": "write_workspace",
        "reason": "No read-only keyword was found; approval is required by default.",
        "matches": [],
    }


def build_codex_prompt(user_prompt: str, risk_level: str = "read_only") -> str:
    prompt = (
        REPORT_INSTRUCTIONS.format(user_prompt=user_prompt).rstrip()
        + "\n\n"
        + REMOTE_EXECUTION_CONSTRAINTS.strip()
        + "\n\n"
        + TASK_EXECUTION_INSTRUCTIONS.format(risk_level=risk_level).strip()
    )
    if risk_level in APPROVAL_RISK_LEVELS:
        prompt += "\n\n" + WRITE_REPORT_INSTRUCTIONS.strip()
    return prompt + "\n"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_job_id() -> str:
    return "job_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def resolve_config_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def tail_text(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Could not read {path.name}: {exc}"
    return "\n".join(lines[-max_lines:])


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path.name}: {exc}"


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def write_text_utf8_sig(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8-sig")


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_allowed_user_id: int
    codex_command: str
    default_sandbox: str
    projects: dict[str, Path]
    jobs_dir: Path
    poll_interval_seconds: float

    @classmethod
    def load(cls) -> "Config":
        if load_dotenv is None:
            raise RuntimeError(
                "python-dotenv is not installed. Run: pip install -r "
                "telegram_codex_controller\\requirements.txt"
            )
        load_dotenv(ENV_PATH)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_user_id = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required in .env")
        if not allowed_user_id:
            raise ValueError("TELEGRAM_ALLOWED_USER_ID is required in .env")
        try:
            allowed_user_id_int = int(allowed_user_id)
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_ID must be an integer") from exc

        default_sandbox = os.getenv("DEFAULT_SANDBOX", "workspace-write").strip()
        if default_sandbox == "danger-full-access":
            raise ValueError("danger-full-access is not allowed by this controller")

        projects = {
            "main": resolve_config_path(
                os.getenv("PROJECT_MAIN", r"D:\LocalProject")
            ),
            "secondary": resolve_config_path(
                os.getenv("PROJECT_SECONDARY", r"D:\OtherProject")
            ),
        }
        jobs_dir = resolve_config_path(
            os.getenv("JOBS_DIR", r"telegram_codex_controller\jobs")
        )
        try:
            poll_interval = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))
        except ValueError as exc:
            raise ValueError("POLL_INTERVAL_SECONDS must be a number") from exc
        if poll_interval <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS must be greater than zero")

        return cls(
            telegram_bot_token=token,
            telegram_allowed_user_id=allowed_user_id_int,
            codex_command=os.getenv("CODEX_COMMAND", "codex").strip() or "codex",
            default_sandbox=default_sandbox,
            projects=projects,
            jobs_dir=jobs_dir,
            poll_interval_seconds=poll_interval,
        )


def build_codex_command(config: Config, args: list[str]) -> list[str]:
    if config.codex_command.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", config.codex_command, "exec", *args]
    return [config.codex_command, "exec", *args]


class TelegramClient:
    def __init__(self, token: str) -> None:
        if requests is None:
            raise RuntimeError(
                "requests is not installed. Run: pip install -r "
                "telegram_codex_controller\\requirements.txt"
            )
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def get_updates(
        self,
        offset: int | None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        response = self.session.get(
            f"{self.base_url}/getUpdates", params=params, timeout=timeout + 10
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        return payload.get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        response = self.session.post(
            f"{self.base_url}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        response.raise_for_status()

    def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        with path.open("rb") as handle:
            response = self.session.post(
                f"{self.base_url}/sendDocument",
                data=data,
                files={"document": (path.name, handle)},
                timeout=120,
            )
        response.raise_for_status()


@dataclass
class Job:
    job_id: str
    project_key: str
    project_dir: Path
    job_dir: Path
    prompt_path: Path
    codex_prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    report_path: Path
    diff_stat_path: Path
    metadata_path: Path
    started_at: str
    chat_id: int | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    status: str = "running"
    risk_level: str = "read_only"
    risk_reason: str = ""
    job_type: str = "codex"
    local_train_alias: str = ""
    local_train_config_path: str = ""
    run_name: str = ""
    command_preview: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    git_status_short: str = ""
    approval_attempted_at: str | None = None
    approved_anyway: bool = False
    returncode: int | None = None
    ended_at: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project_key": self.project_key,
            "project_dir": str(self.project_dir),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "risk_level": self.risk_level,
            "risk_reason": self.risk_reason,
            "job_type": self.job_type,
            "local_train_alias": self.local_train_alias,
            "local_train_config_path": self.local_train_config_path,
            "run_name": self.run_name,
            "command_preview": self.command_preview,
            "blocked_reason": self.blocked_reason,
            "git_status_short": self.git_status_short,
            "approval_attempted_at": self.approval_attempted_at,
            "approved_anyway": self.approved_anyway,
            "returncode": self.returncode,
        }

    def save_metadata(self) -> None:
        self.metadata_path.write_text(
            json.dumps(self.metadata(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class CodexController:
    def __init__(self, config: Config, telegram: TelegramClient | None = None) -> None:
        self.config = config
        self.telegram = telegram
        self.lock = threading.Lock()
        self.current_job: Job | None = None
        self.last_job: Job | None = self._load_last_job()

    def handle_message(self, message: dict[str, Any]) -> str | None:
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        text = message.get("text", "")
        if chat_id is None or user_id is None or not isinstance(text, str):
            return None
        if int(user_id) != self.config.telegram_allowed_user_id:
            self._send(chat_id, "unauthorized")
            return "unauthorized"

        command, arg_text = self._split_command(text)
        if command == "/help":
            response = self.help_text()
            self._send(chat_id, response)
            return response
        if command == "/status":
            response = self.status_text()
            self._send(chat_id, response)
            return response
        if command == "/run":
            response = self.start_run(chat_id, arg_text)
            self._send(chat_id, response)
            return response
        if command == "/train_local":
            response = self.start_local_train(chat_id, arg_text)
            self._send(chat_id, response)
            return response
        if command == "/approve":
            response = self.approve_job(chat_id, arg_text)
            self._send(chat_id, response)
            return response
        if command == "/approve_anyway":
            response = self.approve_anyway_job(chat_id, arg_text)
            self._send(chat_id, response)
            return response
        if command == "/deny":
            response = self.deny_job(chat_id, arg_text)
            self._send(chat_id, response)
            return response
        if command == "/cancel":
            response = self.cancel_current_job()
            self._send(chat_id, response)
            return response
        if command == "/report":
            response = self.resend_report(chat_id)
            if response:
                self._send(chat_id, response)
            return response

        response = "Unknown command. Use /help."
        self._send(chat_id, response)
        return response

    def help_text(self) -> str:
        return (
            "Available commands:\n"
            "/help - Show this help.\n"
            "/run <project> <prompt> - Start a Codex job. Projects: main, secondary.\n"
            "/train_local <alias> - Start an approved local training job "
            "from a whitelist config.\n"
            "/approve [job_id] - Approve the pending job.\n"
            "/approve_anyway [job_id] - Run a blocked job despite "
            "existing repo changes.\n"
            "/deny [job_id] - Deny the pending job.\n"
            "/status - Show controller status.\n"
            "/cancel - Stop the running Codex job.\n"
            "/report - Resend the latest report.\n\n"
            "/approve only starts the job if the repo is clean.\n"
            "/approve_anyway is required when the repo has existing "
            "uncommitted changes."
        )

    def status_text(self) -> str:
        with self.lock:
            current = self.current_job
            last = self.last_job
        if current:
            if current.status == "pending_approval":
                return (
                    "pending_approval\n"
                    f"Job: {current.job_id}\n"
                    f"Project: {current.project_key}\n"
                    f"Risk: {current.risk_level}\n"
                    "Job has NOT started.\n\n"
                    "Options:\n"
                    "/approve\n"
                    "/deny"
                )
            if current.status == "blocked_repo_dirty":
                reason = current.blocked_reason or "repository has uncommitted changes"
                return (
                    "blocked_repo_dirty\n"
                    f"Job: {current.job_id}\n"
                    f"Project: {current.project_key}\n"
                    f"Risk: {current.risk_level}\n"
                    "Job has NOT started.\n\n"
                    "Reason:\n"
                    f"{reason}\n\n"
                    "Options:\n"
                    "/approve_anyway\n"
                    "/deny"
                )
            if current.job_type == "local_train" and current.status == "running":
                started = datetime.fromisoformat(current.started_at)
                runtime_seconds = max(
                    0,
                    int(
                        (
                            datetime.now(timezone.utc)
                            - started.astimezone(timezone.utc)
                        ).total_seconds()
                    ),
                )
                return (
                    "running\n"
                    f"Job: {current.job_id}\n"
                    f"Alias: {current.local_train_alias}\n"
                    f"Config: {current.local_train_config_path}\n"
                    f"Runtime: {runtime_seconds}s"
                )
            started = datetime.fromisoformat(current.started_at)
            runtime_seconds = max(
                0,
                int(
                    (
                        datetime.now(timezone.utc)
                        - started.astimezone(timezone.utc)
                    ).total_seconds()
                ),
            )
            return (
                f"{current.status}\n"
                f"Job: {current.job_id}\n"
                f"Project: {current.project_key}\n"
                f"Risk: {current.risk_level}\n"
                f"Started: {current.started_at}\n"
                f"Runtime: {runtime_seconds}s"
            )
        if last is None:
            return "idle"
        if last.status == "success":
            return f"last_success\nJob: {last.job_id}\nProject: {last.project_key}"
        if last.status in {"failed", "cancelled", "denied", "rejected"}:
            return (
                f"last_failed\n"
                f"Job: {last.job_id}\n"
                f"Project: {last.project_key}\n"
                f"Status: {last.status}"
            )
        return "idle"

    def start_run(self, chat_id: int, arg_text: str) -> str:
        project_key, user_prompt = self._parse_run_args(arg_text)
        if not project_key or not user_prompt:
            return "Usage: /run <project> <prompt>\nProjects: main, secondary"
        if project_key not in self.config.projects:
            return "Unknown project. Allowed projects: main, secondary"

        project_dir = self.config.projects[project_key].resolve()
        if not project_dir.exists() or not project_dir.is_dir():
            return f"Project directory is not available: {project_dir}"

        risk = classify_prompt_risk(user_prompt)
        risk_level = str(risk["level"])
        risk_reason = str(risk["reason"])

        with self.lock:
            if self.current_job is not None:
                return "A job is already running. Use /status or /cancel."
            if risk_level == "dangerous":
                job = self._create_job(
                    chat_id,
                    project_key,
                    project_dir,
                    user_prompt,
                    risk_level,
                    risk_reason,
                    "rejected",
                )
                job.ended_at = utc_now_iso()
                job.save_metadata()
                self.last_job = job
                return (
                    "Task rejected\n"
                    f"Job: {job.job_id}\n"
                    f"Risk: {risk_level}\n"
                    f"Reason: {risk_reason}"
                )

            status = (
                "pending_approval"
                if risk_level in APPROVAL_RISK_LEVELS
                else "running"
            )
            job = self._create_job(
                chat_id,
                project_key,
                project_dir,
                user_prompt,
                risk_level,
                risk_reason,
                status,
            )
            self.current_job = job

        if job.status == "pending_approval":
            return self._approval_required_message(job)

        self._start_job_thread(job)
        return (
            "🚀 Codex job started\n"
            f"Job: {job.job_id}\n"
            f"Project: {job.project_key}"
        )

    def start_local_train(self, chat_id: int, arg_text: str) -> str:
        alias = arg_text.strip().lower()
        if not alias:
            return (
                "Usage: /train_local <alias>\nAliases: "
                + self._local_train_alias_list()
            )
        if alias not in LOCAL_TRAIN_CONFIG_ALIASES:
            return (
                f"Unknown local training alias: {alias}\n"
                f"Aliases: {self._local_train_alias_list()}"
            )

        project_dir = self.config.projects.get(LOCAL_TRAIN_PROJECT_KEY)
        if project_dir is None:
            return (
                "Local training project is not configured. "
                "Missing project key: main"
            )
        project_dir = project_dir.resolve()
        if not project_dir.exists() or not project_dir.is_dir():
            return f"Local training project directory is not available: {project_dir}"

        config_path = (project_dir / LOCAL_TRAIN_CONFIG_ALIASES[alias]).resolve()
        if not config_path.exists() or not config_path.is_file():
            return f"Local training config file is missing: {config_path}"

        python_path = project_dir / LOCAL_TRAIN_PYTHON_RELATIVE
        if not python_path.exists() or not python_path.is_file():
            return f"Local training Python executable is missing: {python_path}"

        train_script_path = project_dir / LOCAL_TRAIN_SCRIPT_RELATIVE
        if not train_script_path.exists() or not train_script_path.is_file():
            return f"Local training script is missing: {train_script_path}"

        run_name = self._local_train_run_name(alias)
        with self.lock:
            if self.current_job is not None:
                return "A job is already running. Use /status or /cancel."
            job = self._create_local_train_job(
                chat_id=chat_id,
                project_dir=project_dir,
                alias=alias,
                config_path=config_path,
                run_name=run_name,
            )
            self.current_job = job

        return (
            self._approval_required_message(job)
            + "\n\nLocal training request:\n"
            f"Alias: {alias}\n"
            f"Config: {config_path}\n"
            f"Run name: {run_name}"
        )

    def approve_job(self, chat_id: int, arg_text: str) -> str:
        job_id = arg_text.strip()
        with self.lock:
            job, error = self._resolve_approval_job_locked(
                job_id,
                {"pending_approval"},
                "No pending approval job.",
            )
        if error:
            return error
        if job is None:
            return "No pending approval job."
        if job.chat_id != chat_id:
            return "Job belongs to a different chat."

        if job.risk_level in APPROVAL_RISK_LEVELS:
            git_status = self._git_status_short(job.project_dir)
            if git_status["error"]:
                return (
                    "Approval was NOT applied.\n"
                    "Job has NOT started.\n\n"
                    f"Job: {job.job_id}\n"
                    "Status: pending_approval\n"
                    "Reason: could not verify git status.\n\n"
                    f"{git_status['error']}"
                )
            if git_status["has_changes"]:
                with self.lock:
                    if self.current_job is not job or job.status != "pending_approval":
                        return "Pending job changed before approval could be recorded."
                    job.status = "blocked_repo_dirty"
                    job.blocked_reason = "repository has uncommitted changes"
                    job.git_status_short = str(git_status["output"])
                    job.approval_attempted_at = utc_now_iso()
                    job.save_metadata()
                return self._repo_dirty_blocked_message(job)

        with self.lock:
            if self.current_job is not job or job.status != "pending_approval":
                return "Pending job changed before approval could start."
            job.status = "running"
            job.started_at = utc_now_iso()
            job.approval_attempted_at = utc_now_iso()
            job.save_metadata()

        self._start_job_thread(job)
        return self._job_started_message(job)

    def approve_anyway_job(self, chat_id: int, arg_text: str) -> str:
        job_id = arg_text.strip()
        with self.lock:
            job, error = self._resolve_approval_job_locked(
                job_id,
                {"pending_approval", "blocked_repo_dirty"},
                "No pending approval job.",
            )
        if error:
            return error
        if job is None:
            return "No pending approval job."
        if job.chat_id != chat_id:
            return "Job belongs to a different chat."

        with self.lock:
            if self.current_job is not job or job.status not in {
                "pending_approval",
                "blocked_repo_dirty",
            }:
                return "Approval job changed before it could start."
            job.status = "running"
            job.started_at = utc_now_iso()
            job.approval_attempted_at = job.approval_attempted_at or utc_now_iso()
            job.approved_anyway = True
            job.save_metadata()

        self._start_job_thread(job)
        if job.job_type == "local_train":
            return (
                "⚠️ Running despite existing repo changes.\n"
                + self._local_training_started_message(job)
            )
        return (
            "⚠️ Running despite existing repo changes.\n"
            f"Job: {job.job_id}\n"
            "Status: running"
        )

    def deny_job(self, chat_id: int, arg_text: str) -> str:
        job_id = arg_text.strip()
        with self.lock:
            job, error = self._resolve_approval_job_locked(
                job_id,
                {"pending_approval", "blocked_repo_dirty"},
                "No pending approval job.",
            )
            if error:
                return error
            if job is None:
                return "No pending approval job."
            if job.chat_id != chat_id:
                return "Job belongs to a different chat."
            job.status = "denied"
            job.ended_at = utc_now_iso()
            job.save_metadata()
            self.current_job = None
            self.last_job = job
        return f"Job denied.\nJob: {job.job_id}\nStatus: denied"

    def _approval_jobs_locked(self, allowed_statuses: set[str]) -> list[Job]:
        if (
            self.current_job is not None
            and self.current_job.status in allowed_statuses
        ):
            return [self.current_job]
        return []

    def _resolve_approval_job_locked(
        self,
        job_id: str,
        allowed_statuses: set[str],
        no_job_message: str,
    ) -> tuple[Job | None, str | None]:
        pending_jobs = self._approval_jobs_locked(allowed_statuses)
        if not pending_jobs:
            if self.current_job is not None:
                if self.current_job.status == "blocked_repo_dirty":
                    return (
                        None,
                        "Job is blocked_repo_dirty. Use /approve_anyway or /deny.",
                    )
                return None, f"Job is {self.current_job.status}."
            return None, no_job_message
        if not job_id:
            if len(pending_jobs) > 1:
                return (
                    None,
                    "Multiple pending jobs found. Please use "
                    "/approve <job_id> or /deny <job_id>.",
                )
            return pending_jobs[0], None
        for job in pending_jobs:
            if job.job_id == job_id:
                return job, None
        pending_ids = ", ".join(job.job_id for job in pending_jobs)
        return None, f"Job id mismatch. Pending job: {pending_ids}"

    def cancel_current_job(self) -> str:
        with self.lock:
            job = self.current_job
        if job is None:
            return "No job is running."
        if job.status == "pending_approval":
            return f"Job is pending approval. Use /deny {job.job_id} to deny it."
        if job.status == "blocked_repo_dirty":
            return (
                "Job is blocked_repo_dirty. Use /approve_anyway "
                f"or /deny {job.job_id}."
            )
        if job.process is None:
            return "Job is starting. Try /cancel again."
        job.status = "cancelled"
        self._terminate_process(job.process)
        if job.job_type == "local_train":
            return f"🛑 Local training cancelled\nJob: {job.job_id}"
        return f"🛑 Codex job cancelled\nJob: {job.job_id}"

    def resend_report(self, chat_id: int) -> str:
        with self.lock:
            job = self.last_job
        if job is None:
            return "No report is available."
        self._ensure_report_exists(job)
        if job.report_path.exists():
            self._send_report_document(chat_id, job)
            return f"Report resent.\nJob: {job.job_id}"
        fallback = self._fallback_output(job)
        return f"No report.md found for {job.job_id}.\n\n{fallback}"

    def _create_job(
        self,
        chat_id: int,
        project_key: str,
        project_dir: Path,
        user_prompt: str,
        risk_level: str,
        risk_reason: str,
        status: str,
    ) -> Job:
        self.config.jobs_dir.mkdir(parents=True, exist_ok=True)
        job_id = local_job_id()
        job_dir = self.config.jobs_dir / job_id
        suffix = 1
        while job_dir.exists():
            suffix += 1
            job_id = f"{local_job_id()}_{suffix}"
            job_dir = self.config.jobs_dir / job_id
        job_dir.mkdir(parents=True)

        codex_prompt = build_codex_prompt(user_prompt, risk_level)
        prompt_path = job_dir / "prompt.txt"
        codex_prompt_path = job_dir / "codex_prompt.txt"
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        report_path = job_dir / "report.md"
        diff_stat_path = job_dir / "diff_stat.txt"
        metadata_path = job_dir / "metadata.json"

        write_text(prompt_path, user_prompt)
        write_text(codex_prompt_path, codex_prompt)

        job = Job(
            job_id=job_id,
            project_key=project_key,
            project_dir=project_dir,
            job_dir=job_dir,
            prompt_path=prompt_path,
            codex_prompt_path=codex_prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            report_path=report_path,
            diff_stat_path=diff_stat_path,
            metadata_path=metadata_path,
            started_at=utc_now_iso(),
            chat_id=chat_id,
            status=status,
            risk_level=risk_level,
            risk_reason=risk_reason,
        )
        job.save_metadata()
        return job

    def _create_local_train_job(
        self,
        chat_id: int,
        project_dir: Path,
        alias: str,
        config_path: Path,
        run_name: str,
    ) -> Job:
        self.config.jobs_dir.mkdir(parents=True, exist_ok=True)
        job_id = local_job_id()
        job_dir = self.config.jobs_dir / job_id
        suffix = 1
        while job_dir.exists():
            suffix += 1
            job_id = f"{local_job_id()}_{suffix}"
            job_dir = self.config.jobs_dir / job_id
        job_dir.mkdir(parents=True)

        prompt_path = job_dir / "prompt.txt"
        codex_prompt_path = job_dir / "codex_prompt.txt"
        stdout_path = job_dir / "train_stdout.log"
        stderr_path = job_dir / "train_stderr.log"
        report_path = job_dir / "report.md"
        diff_stat_path = job_dir / "diff_stat.txt"
        metadata_path = job_dir / "metadata.json"

        write_text(
            prompt_path,
            (
                "Local training request\n"
                f"Alias: {alias}\n"
                f"Config: {config_path}\n"
                f"Run name: {run_name}\n"
            ),
        )
        write_text(codex_prompt_path, "Local training is run directly by the controller.\n")

        job = Job(
            job_id=job_id,
            project_key=LOCAL_TRAIN_PROJECT_KEY,
            project_dir=project_dir,
            job_dir=job_dir,
            prompt_path=prompt_path,
            codex_prompt_path=codex_prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            report_path=report_path,
            diff_stat_path=diff_stat_path,
            metadata_path=metadata_path,
            started_at=utc_now_iso(),
            chat_id=chat_id,
            status="pending_approval",
            risk_level="run_expensive",
            risk_reason="Local training requires approval.",
            job_type="local_train",
            local_train_alias=alias,
            local_train_config_path=str(config_path),
            run_name=run_name,
        )
        job.save_metadata()
        return job

    @staticmethod
    def _local_train_alias_list() -> str:
        return ", ".join(sorted(LOCAL_TRAIN_CONFIG_ALIASES))

    @staticmethod
    def _local_train_run_name(alias: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"local_train_{alias}_{timestamp}"

    @staticmethod
    def _approval_required_message(job: Job) -> str:
        extra = ""
        if job.risk_level == "run_expensive":
            extra = "\nThis task may take a long time or use substantial compute."
        return (
            "⚠️ Approval required\n"
            f"Job: {job.job_id}\n"
            "Status: pending_approval\n"
            f"Risk: {job.risk_level}\n"
            "Job has NOT started.\n"
            f"Reason: {job.risk_reason}"
            f"{extra}\n\n"
            "Reply:\n"
            "/approve\n"
            "or\n"
            "/deny\n\n"
            "Advanced:\n"
            f"/approve {job.job_id}\n"
            f"/deny {job.job_id}"
        )

    @staticmethod
    def _repo_dirty_blocked_message(job: Job) -> str:
        return (
            "⚠️ Approval was NOT applied.\n"
            "Job has NOT started.\n\n"
            f"Job: {job.job_id}\n"
            "Status: blocked_repo_dirty\n"
            "Reason: repository has uncommitted changes.\n\n"
            "git status --short:\n"
            f"{job.git_status_short[:2500]}\n\n"
            "Options:\n"
            "/approve_anyway\n"
            "/deny"
        )

    def _job_started_message(self, job: Job) -> str:
        if job.job_type == "local_train":
            return self._local_training_started_message(job)
        return (
            "running\n"
            "Codex job approved and started.\n"
            f"Job: {job.job_id}\n"
            f"Project: {job.project_key}\n"
            f"Risk: {job.risk_level}"
        )

    @staticmethod
    def _local_training_started_message(job: Job) -> str:
        return (
            "🚀 Local training started\n"
            f"Job: {job.job_id}\n"
            f"Alias: {job.local_train_alias}\n"
            f"Config: {job.local_train_config_path}\n"
            f"Run name: {job.run_name}"
        )

    def _start_job_thread(self, job: Job) -> None:
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()

    @staticmethod
    def _git_status_short(project_dir: Path) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                shell=False,
                timeout=30,
            )
        except Exception as exc:
            return {"has_changes": True, "output": "", "error": f"{type(exc).__name__}: {exc}"}
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            return {
                "has_changes": True,
                "output": output,
                "error": f"git status --short failed with code {completed.returncode}: {output}",
            }
        return {"has_changes": bool(output), "output": output or "(clean)", "error": ""}

    def _ensure_report_exists(self, job: Job) -> None:
        if job.report_path.exists():
            return
        stdout = read_text_if_exists(job.stdout_path)
        stderr = read_text_if_exists(job.stderr_path)
        source_name = "stdout.log"
        body = stdout.strip()
        if not body:
            source_name = "stderr.log"
            body = stderr.strip()
        if not body:
            body = "No stdout or stderr output was captured."
        report = (
            "# Codex Fallback Report\n\n"
            "## Task Summary\n"
            "Codex did not create report.md, so the controller generated this fallback report.\n\n"
            f"## Source\n{source_name}\n\n"
            "## Output\n"
            "```text\n"
            f"{body}\n"
            "```\n"
        )
        write_text_utf8_sig(job.report_path, report)

    @staticmethod
    def _telegram_report_path(job: Job) -> Path:
        return job.job_dir / "report_telegram.md"

    def _prepare_telegram_report(self, job: Job) -> Path:
        self._ensure_report_exists(job)
        report_text = job.report_path.read_text(encoding="utf-8", errors="replace")
        telegram_report_path = self._telegram_report_path(job)
        write_text_utf8_sig(telegram_report_path, report_text)
        return telegram_report_path

    def _send_report_document(self, chat_id: int, job: Job) -> None:
        telegram_report_path = self._prepare_telegram_report(job)
        self._send_document(chat_id, telegram_report_path, "Codex report")

    def _write_diff_stat(self, job: Job) -> None:
        if job.risk_level not in APPROVAL_RISK_LEVELS:
            return
        try:
            completed = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=str(job.project_dir),
                capture_output=True,
                text=True,
                shell=False,
                timeout=30,
            )
        except Exception as exc:
            write_text(job.diff_stat_path, f"{type(exc).__name__}: {exc}\n")
            return
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            output = f"git diff --stat failed with code {completed.returncode}:\n{output}"
        write_text(job.diff_stat_path, output + "\n" if output else "(no diff)\n")

    def _run_job(self, job: Job) -> None:
        if job.job_type == "local_train":
            self._run_local_training(job)
            return

        prompt_text = job.codex_prompt_path.read_text(encoding="utf-8")
        codex_args = [
            "--cd",
            str(job.project_dir),
            "--sandbox",
            self.config.default_sandbox,
            "-o",
            str(job.report_path),
            prompt_text,
        ]
        command = build_codex_command(self.config, codex_args)
        job.command_preview = build_codex_command(
            self.config,
            [*codex_args[:-1], "<codex prompt text omitted; see codex_prompt.txt>"],
        )
        job.save_metadata()

        try:
            with job.stdout_path.open("w", encoding="utf-8", errors="replace") as stdout:
                with job.stderr_path.open(
                    "w", encoding="utf-8", errors="replace"
                ) as stderr:
                    job.process = subprocess.Popen(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        text=True,
                        shell=False,
                        cwd=str(job.project_dir),
                        creationflags=self._creation_flags(),
                    )
                    returncode = job.process.wait()
        except Exception as exc:
            write_text(job.stderr_path, f"{type(exc).__name__}: {exc}\n")
            returncode = -1

        if job.status == "cancelled":
            status = "cancelled"
        elif returncode == 0:
            status = "success"
        else:
            status = "failed"

        job.status = status
        job.returncode = returncode
        job.ended_at = utc_now_iso()
        job.save_metadata()
        self._ensure_report_exists(job)
        self._write_diff_stat(job)

        with self.lock:
            self.current_job = None
            self.last_job = job

        if job.chat_id is not None and job.status != "cancelled":
            self._notify_job_finished(job)

    def _run_local_training(self, job: Job) -> None:
        python_path = job.project_dir / LOCAL_TRAIN_PYTHON_RELATIVE
        train_script = str(LOCAL_TRAIN_SCRIPT_RELATIVE)
        command = [
            str(python_path),
            train_script,
            "--config",
            job.local_train_config_path,
            "--run-name",
            job.run_name,
        ]
        job.command_preview = command
        job.save_metadata()

        started = datetime.fromisoformat(job.started_at)
        try:
            with job.stdout_path.open("w", encoding="utf-8", errors="replace") as stdout:
                with job.stderr_path.open(
                    "w", encoding="utf-8", errors="replace"
                ) as stderr:
                    job.process = subprocess.Popen(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        text=True,
                        shell=False,
                        cwd=str(job.project_dir),
                        creationflags=self._creation_flags(),
                    )
                    returncode = job.process.wait()
        except Exception as exc:
            write_text(job.stderr_path, f"{type(exc).__name__}: {exc}\n")
            returncode = -1

        if job.status == "cancelled":
            status = "cancelled"
        elif returncode == 0:
            status = "success"
        else:
            status = "failed"

        job.status = status
        job.returncode = returncode
        job.ended_at = utc_now_iso()
        job.save_metadata()
        self._write_local_training_report(job, command, started)

        with self.lock:
            self.current_job = None
            self.last_job = job

        if job.chat_id is not None and job.status != "cancelled":
            self._notify_local_training_finished(job)

    def _write_local_training_report(
        self, job: Job, command: list[str], started: datetime
    ) -> None:
        ended = (
            datetime.fromisoformat(job.ended_at)
            if job.ended_at
            else datetime.now(timezone.utc)
        )
        duration_seconds = max(
            0,
            int(
                (
                    ended.astimezone(timezone.utc)
                    - started.astimezone(timezone.utc)
                ).total_seconds()
            ),
        )
        stdout_tail = tail_text(job.stdout_path, max_lines=80)
        stderr_tail = tail_text(job.stderr_path, max_lines=80)
        possible_paths = [
            str(job.project_dir / "outputs"),
            f"Search for run name: {job.run_name}",
        ]
        report = (
            "# Local Training Report\n\n"
            "## 1. Task Summary\n"
            "Local training was launched directly by the Telegram controller.\n\n"
            "## 2. Alias\n"
            f"{job.local_train_alias}\n\n"
            "## 3. Config path\n"
            f"{job.local_train_config_path}\n\n"
            "## 4. Run name\n"
            f"{job.run_name}\n\n"
            "## 5. Command executed\n"
            "```text\n"
            f"{' '.join(command)}\n"
            "```\n\n"
            "## 6. Return code\n"
            f"{job.returncode}\n\n"
            "## 7. Started at / ended at / duration\n"
            f"Started: {job.started_at}\n"
            f"Ended: {job.ended_at}\n"
            f"Duration: {duration_seconds}s\n\n"
            "## 8. Last 80 lines of stdout\n"
            "```text\n"
            f"{stdout_tail or '(empty)'}\n"
            "```\n\n"
            "## 9. Last 80 lines of stderr\n"
            "```text\n"
            f"{stderr_tail or '(empty)'}\n"
            "```\n\n"
            "## 10. Possible output paths\n"
            + "\n".join(f"- {path}" for path in possible_paths)
            + "\n\n"
            "## 11. Suggested next steps\n"
            "- Inspect the stdout/stderr tails above.\n"
            "- Check the run output path matching the run name.\n"
            "- Run the relevant validation before promoting an output artifact.\n"
        )
        write_text_utf8_sig(job.report_path, report)

    def _notify_job_finished(self, job: Job) -> None:
        if job.status == "success":
            self._send(
                job.chat_id,
                "✅ Codex job finished\n"
                f"Job: {job.job_id}\n"
                f"Project: {job.project_key}\n"
                "Status: success\n"
                "Report attached.",
            )
            if job.report_path.exists():
                self._send_report_document(job.chat_id, job)
            else:
                self._send(job.chat_id, self._fallback_output(job))
            self._send_diff_stat_summary(job)
            return

        if job.status == "cancelled":
            self._send(job.chat_id, f"🛑 Codex job cancelled\nJob: {job.job_id}")
            return

        self._send(
            job.chat_id,
            "❌ Codex job failed\n"
            f"Job: {job.job_id}\n"
            f"Project: {job.project_key}\n"
            f"Return code: {job.returncode}\n"
            "stderr attached or summarized.",
        )
        if job.stderr_path.exists() and job.stderr_path.stat().st_size > 0:
            self._send_document(job.chat_id, job.stderr_path, "stderr.log")
        else:
            self._send(job.chat_id, self._fallback_output(job))
        if job.report_path.exists():
            self._send_report_document(job.chat_id, job)
        self._send_diff_stat_summary(job)

    def _notify_local_training_finished(self, job: Job) -> None:
        if job.status == "success":
            title = "✅ Local training finished"
        else:
            title = "❌ Local training failed"
        stdout_tail = tail_text(job.stdout_path, max_lines=80)
        stderr_tail = tail_text(job.stderr_path, max_lines=80)
        summary_tail = stdout_tail if job.status == "success" and stdout_tail else stderr_tail
        if not summary_tail:
            summary_tail = stdout_tail or "No stdout/stderr output is available."
        self._send(
            job.chat_id,
            f"{title}\n"
            f"Job: {job.job_id}\n"
            f"Alias: {job.local_train_alias}\n"
            f"Run name: {job.run_name}\n"
            f"Return code: {job.returncode}\n\n"
            "Log summary:\n"
            f"{summary_tail[:3000]}",
        )
        if job.report_path.exists():
            self._send_report_document(job.chat_id, job)

    def _send_diff_stat_summary(self, job: Job) -> None:
        if job.chat_id is None or job.risk_level not in APPROVAL_RISK_LEVELS:
            return
        if not job.diff_stat_path.exists():
            return
        diff_stat = tail_text(job.diff_stat_path, max_lines=80)
        self._send(
            job.chat_id,
            "Git diff summary\n"
            f"Job: {job.job_id}\n"
            f"```text\n{diff_stat[:3000]}\n```",
        )

    def _fallback_output(self, job: Job) -> str:
        stderr_tail = tail_text(job.stderr_path)
        stdout_tail = tail_text(job.stdout_path)
        chunks = []
        if stderr_tail:
            chunks.append("stderr tail:\n" + stderr_tail)
        if stdout_tail:
            chunks.append("stdout tail:\n" + stdout_tail)
        return "\n\n".join(chunks)[:3500] or "No stdout/stderr output is available."

    def _load_last_job(self) -> Job | None:
        jobs_dir = self.config.jobs_dir
        if not jobs_dir.exists():
            return None
        metadata_files = sorted(jobs_dir.glob("job_*/metadata.json"))
        for metadata_path in reversed(metadata_files):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_dir = metadata_path.parent
            job_type = metadata.get("job_type", "codex")
            stdout_name = "train_stdout.log" if job_type == "local_train" else "stdout.log"
            stderr_name = "train_stderr.log" if job_type == "local_train" else "stderr.log"
            return Job(
                job_id=metadata.get("job_id", job_dir.name),
                project_key=metadata.get("project_key", ""),
                project_dir=Path(metadata.get("project_dir", "")),
                job_dir=job_dir,
                prompt_path=job_dir / "prompt.txt",
                codex_prompt_path=job_dir / "codex_prompt.txt",
                stdout_path=job_dir / stdout_name,
                stderr_path=job_dir / stderr_name,
                report_path=job_dir / "report.md",
                diff_stat_path=job_dir / "diff_stat.txt",
                metadata_path=metadata_path,
                started_at=metadata.get("started_at", ""),
                status=metadata.get("status", "failed"),
                risk_level=metadata.get("risk_level", "read_only"),
                risk_reason=metadata.get("risk_reason", ""),
                job_type=job_type,
                local_train_alias=metadata.get("local_train_alias", ""),
                local_train_config_path=metadata.get("local_train_config_path", ""),
                run_name=metadata.get("run_name", ""),
                command_preview=metadata.get("command_preview", []),
                blocked_reason=metadata.get("blocked_reason", ""),
                git_status_short=metadata.get("git_status_short", ""),
                approval_attempted_at=metadata.get("approval_attempted_at"),
                approved_anyway=bool(metadata.get("approved_anyway", False)),
                returncode=metadata.get("returncode"),
                ended_at=metadata.get("ended_at"),
            )
        return None

    def _send(self, chat_id: int, text: str) -> None:
        if self.telegram is not None:
            self.telegram.send_message(chat_id, text)

    def _send_document(self, chat_id: int, path: Path, caption: str | None = None) -> None:
        if self.telegram is not None:
            self.telegram.send_document(chat_id, path, caption)

    @staticmethod
    def _split_command(text: str) -> tuple[str, str]:
        stripped = text.strip()
        if not stripped:
            return "", ""
        first, _, rest = stripped.partition(" ")
        command = first.split("@", 1)[0].lower()
        return command, rest.strip()

    @staticmethod
    def _parse_run_args(arg_text: str) -> tuple[str | None, str | None]:
        project_key, sep, user_prompt = arg_text.strip().partition(" ")
        if not sep:
            return None, None
        return project_key.lower(), user_prompt.strip()

    @staticmethod
    def _creation_flags() -> int:
        if os.name == "nt":
            return subprocess.CREATE_NEW_PROCESS_GROUP
        return 0

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                process.terminate()
            else:
                try:
                    process.wait(timeout=10)
                    return
                except subprocess.TimeoutExpired:
                    process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def run_polling(config: Config) -> None:
    telegram = TelegramClient(config.telegram_bot_token)
    controller = CodexController(config, telegram)
    offset: int | None = None
    print("Telegram Codex controller started. Press Ctrl+C to stop.", flush=True)
    while True:
        try:
            updates = telegram.get_updates(offset=offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if message:
                    controller.handle_message(message)
        except KeyboardInterrupt:
            print("Stopping controller.", flush=True)
            return
        except Exception as exc:
            print(f"Polling error: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(config.poll_interval_seconds)


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        project_dir = tmp_root / "project"
        jobs_dir = tmp_root / "jobs"
        project_dir.mkdir()
        (project_dir / "configs").mkdir()
        for config_relative_path in set(LOCAL_TRAIN_CONFIG_ALIASES.values()):
            write_text(project_dir / config_relative_path, "name: test\n")
        (project_dir / ".venv" / "Scripts").mkdir(parents=True)
        write_text(project_dir / LOCAL_TRAIN_PYTHON_RELATIVE, "")
        (project_dir / "scripts").mkdir()
        write_text(project_dir / LOCAL_TRAIN_SCRIPT_RELATIVE, "print('train')\n")
        config = Config(
            telegram_bot_token="test-token",
            telegram_allowed_user_id=123,
            codex_command="codex",
            default_sandbox="workspace-write",
            projects={
                "main": project_dir,
                "secondary": project_dir,
            },
            jobs_dir=jobs_dir,
            poll_interval_seconds=2,
        )
        assert build_codex_command(config, ["--cd", str(project_dir), "prompt"]) == [
            "codex",
            "exec",
            "--cd",
            str(project_dir),
            "prompt",
        ]
        cmd_config = Config(
            telegram_bot_token="test-token",
            telegram_allowed_user_id=123,
            codex_command=str(tmp_root / "codex.cmd"),
            default_sandbox="workspace-write",
            projects=config.projects,
            jobs_dir=jobs_dir,
            poll_interval_seconds=2,
        )
        assert build_codex_command(
            cmd_config,
            ["--cd", str(project_dir), "prompt"],
        ) == [
            "cmd.exe",
            "/c",
            str(tmp_root / "codex.cmd"),
            "exec",
            "--cd",
            str(project_dir),
            "prompt",
        ]
        bat_config = Config(
            telegram_bot_token="test-token",
            telegram_allowed_user_id=123,
            codex_command=str(tmp_root / "codex.BAT"),
            default_sandbox="workspace-write",
            projects=config.projects,
            jobs_dir=jobs_dir,
            poll_interval_seconds=2,
        )
        assert build_codex_command(bat_config, ["prompt"]) == [
            "cmd.exe",
            "/c",
            str(tmp_root / "codex.BAT"),
            "exec",
            "prompt",
        ]
        class TestCodexController(CodexController):
            def __init__(self, test_config: Config) -> None:
                super().__init__(test_config, telegram=None)
                self.started_jobs: list[str] = []

            def _start_job_thread(self, job: Job) -> None:
                self.started_jobs.append(job.job_id)

            @staticmethod
            def _git_status_short(project_dir: Path) -> dict[str, Any]:
                return {"has_changes": False, "output": "(clean)", "error": ""}

        class DirtyRepoTestCodexController(TestCodexController):
            @staticmethod
            def _git_status_short(project_dir: Path) -> dict[str, Any]:
                return {
                    "has_changes": True,
                    "output": " M README.md\n?? scratch.txt",
                    "error": "",
                }

        def new_controller() -> TestCodexController:
            return TestCodexController(config)

        def new_dirty_controller() -> DirtyRepoTestCodexController:
            return DirtyRepoTestCodexController(config)

        controller = new_controller()
        help_response = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/help"}
        )
        unauthorized = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 999}, "text": "/run main test"}
        )
        status = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/status"}
        )
        assert help_response and "/run <project> <prompt>" in help_response
        assert "/train_local <alias>" in help_response
        assert "/approve [job_id]" in help_response
        assert "/approve_anyway [job_id]" in help_response
        assert "/deny [job_id]" in help_response
        assert "/approve only starts the job if the repo is clean." in help_response
        assert unauthorized == "unauthorized"
        assert status == "idle"
        assert controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve"}
        ) == "No pending approval job."
        assert controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/deny"}
        ) == "No pending approval job."

        train_pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/train_local default"}
        )
        assert train_pending and "Approval required" in train_pending
        assert "Local training request" in train_pending
        assert controller.current_job is not None
        assert controller.current_job.job_type == "local_train"
        assert controller.current_job.status == "pending_approval"
        assert controller.current_job.risk_level == "run_expensive"
        assert controller.current_job.stdout_path.name == "train_stdout.log"
        assert controller.current_job.stderr_path.name == "train_stderr.log"
        train_job_id = controller.current_job.job_id
        train_started = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve"}
        )
        assert train_started and "Local training started" in train_started
        assert f"Job: {train_job_id}" in train_started
        assert "Alias: default" in train_started
        assert controller.started_jobs == [train_job_id]
        assert controller.current_job is not None
        assert controller.current_job.status == "running"
        train_status = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/status"}
        )
        assert train_status and train_status.startswith("running")
        assert "Alias: default" in train_status
        assert "Runtime:" in train_status

        controller = new_controller()
        missing_config = project_dir / LOCAL_TRAIN_CONFIG_ALIASES["resume"]
        missing_config.unlink()
        missing_response = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/train_local resume"}
        )
        assert missing_response and "Local training config file is missing" in missing_response
        write_text(missing_config, "name: test\n")

        controller = new_dirty_controller()
        train_pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/train_local experiment"}
        )
        assert train_pending and "Approval required" in train_pending
        assert controller.current_job is not None
        train_job_id = controller.current_job.job_id
        blocked = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve"}
        )
        assert blocked and "Status: blocked_repo_dirty" in blocked
        assert controller.current_job is not None
        assert controller.current_job.status == "blocked_repo_dirty"
        train_started_anyway = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve_anyway"}
        )
        assert train_started_anyway and "Local training started" in train_started_anyway
        assert controller.started_jobs == [train_job_id]

        controller = new_controller()
        pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main modify README"}
        )
        assert pending and "Approval required" in pending
        assert "Reply:\n/approve\nor\n/deny" in pending
        assert "Advanced:" in pending
        assert controller.current_job is not None
        pending_job_id = controller.current_job.job_id
        assert controller.current_job.status == "pending_approval"
        approved = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve"}
        )
        assert approved and "Codex job approved and started" in approved
        assert controller.started_jobs == [pending_job_id]
        assert controller.current_job is not None
        assert controller.current_job.status == "running"

        controller = new_controller()
        pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main modify README"}
        )
        assert pending and "Approval required" in pending
        assert controller.current_job is not None
        pending_job_id = controller.current_job.job_id
        denied = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/deny"}
        )
        assert denied == f"Job denied.\nJob: {pending_job_id}\nStatus: denied"
        assert controller.current_job is None

        controller = new_controller()
        pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main modify README"}
        )
        assert pending and "Approval required" in pending
        assert controller.current_job is not None
        pending_job_id = controller.current_job.job_id
        approved = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": f"/approve {pending_job_id}"}
        )
        assert approved and "Codex job approved and started" in approved
        assert controller.started_jobs == [pending_job_id]

        controller = new_controller()
        pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main modify README"}
        )
        assert pending and "Approval required" in pending
        assert controller.current_job is not None
        pending_job_id = controller.current_job.job_id
        denied = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": f"/deny {pending_job_id}"}
        )
        assert denied == f"Job denied.\nJob: {pending_job_id}\nStatus: denied"
        assert controller.current_job is None

        controller = new_dirty_controller()
        pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main modify README"}
        )
        assert pending and "Approval required" in pending
        assert controller.current_job is not None
        pending_job_id = controller.current_job.job_id
        blocked = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve"}
        )
        assert blocked and "Approval was NOT applied" in blocked
        assert "Status: blocked_repo_dirty" in blocked
        assert " M README.md" in blocked
        assert controller.current_job is not None
        assert controller.current_job.status == "blocked_repo_dirty"
        assert controller.current_job.blocked_reason == "repository has uncommitted changes"
        assert controller.current_job.git_status_short == " M README.md\n?? scratch.txt"
        assert controller.current_job.approval_attempted_at
        metadata = json.loads(controller.current_job.metadata_path.read_text(encoding="utf-8"))
        assert metadata["status"] == "blocked_repo_dirty"
        assert metadata["blocked_reason"] == "repository has uncommitted changes"
        assert metadata["git_status_short"] == " M README.md\n?? scratch.txt"
        assert metadata["approval_attempted_at"]
        status = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/status"}
        )
        assert status and status.startswith("blocked_repo_dirty")
        assert "Job has NOT started." in status
        assert "/approve_anyway" in status
        assert "/deny" in status
        approved_anyway = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/approve_anyway"}
        )
        assert approved_anyway and "Running despite existing repo changes" in approved_anyway
        assert controller.started_jobs == [pending_job_id]
        assert controller.current_job is not None
        assert controller.current_job.status == "running"
        assert controller.current_job.approved_anyway is True

        controller = new_dirty_controller()
        pending = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main modify README"}
        )
        assert pending and "Approval required" in pending
        assert controller.current_job is not None
        pending_job_id = controller.current_job.job_id
        blocked = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": f"/approve {pending_job_id}"}
        )
        assert blocked and "Status: blocked_repo_dirty" in blocked
        denied = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/deny"}
        )
        assert denied == f"Job denied.\nJob: {pending_job_id}\nStatus: denied"
        assert controller.current_job is None

        controller = new_controller()
        rejected = controller.handle_message(
            {"chat": {"id": 1}, "from": {"id": 123}, "text": "/run main rm -rf outputs"}
        )
        assert rejected and "Task rejected" in rejected

        controller = new_controller()
        report_job = controller._create_job(
            chat_id=1,
            project_key="main",
            project_dir=project_dir,
            user_prompt="inspect",
            risk_level="read_only",
            risk_reason="test",
            status="success",
        )
        report_text = "# Report\n\n\u4e2d\u6587 report"
        write_text(report_job.report_path, report_text)
        original_report_bytes = report_job.report_path.read_bytes()
        assert not original_report_bytes.startswith(b"\xef\xbb\xbf")
        telegram_report_path = controller._prepare_telegram_report(report_job)
        telegram_report_bytes = telegram_report_path.read_bytes()
        assert telegram_report_path.name == "report_telegram.md"
        assert telegram_report_bytes.startswith(b"\xef\xbb\xbf")
        assert telegram_report_path.read_text(encoding="utf-8-sig") == report_text
        assert report_job.report_path.read_bytes() == original_report_bytes

        fallback_job = controller._create_job(
            chat_id=1,
            project_key="main",
            project_dir=project_dir,
            user_prompt="inspect",
            risk_level="read_only",
            risk_reason="test",
            status="failed",
        )
        write_text(fallback_job.stdout_path, "stdout \u4e2d\u6587")
        controller._ensure_report_exists(fallback_job)
        assert fallback_job.report_path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "stdout \u4e2d\u6587" in fallback_job.report_path.read_text(
            encoding="utf-8-sig"
        )

    assert classify_prompt_risk("检查项目并生成报告，不要修改")["level"] == "read_only"
    assert classify_prompt_risk("update the README")["level"] == "write_workspace"
    assert classify_prompt_risk("train the model")["level"] == "run_expensive"
    assert classify_prompt_risk("git reset --hard")["level"] == "dangerous"

    codex_prompt = build_codex_prompt("inspect the project")
    write_prompt = build_codex_prompt("modify README", "write_workspace")
    assert "Remote execution constraints" in codex_prompt
    assert ".venv/" in codex_prompt
    assert "Get-ChildItem -Recurse" in codex_prompt
    assert "rg --files" in codex_prompt
    assert "Git diff summary" in write_prompt
    assert "Do not leave critical task steps for the user to run manually" in codex_prompt
    assert "For read_only tasks, do not start training" in codex_prompt
    assert "For run_expensive tasks that the user approved" in build_codex_prompt(
        "train", "run_expensive"
    )
    print("self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Telegram controller for Codex CLI")
    parser.add_argument("--self-test", action="store_true", help="run offline checks")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    config = Config.load()
    run_polling(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
