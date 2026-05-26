from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
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


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


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
            "gpt": resolve_config_path(os.getenv("PROJECT_GPT", r"D:\GPT_Development")),
            "endoflip": resolve_config_path(os.getenv("PROJECT_ENDOFLIP", r"D:\EndoFLIP")),
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


class TelegramClient:
    def __init__(self, token: str) -> None:
        if requests is None:
            raise RuntimeError(
                "requests is not installed. Run: pip install -r "
                "telegram_codex_controller\\requirements.txt"
            )
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
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

    def send_document(self, chat_id: int, path: Path, caption: str | None = None) -> None:
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
    metadata_path: Path
    started_at: str
    chat_id: int | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    status: str = "running"
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
            "/run <project> <prompt> - Start a Codex job. Projects: gpt, endoflip.\n"
            "/status - Show controller status.\n"
            "/cancel - Stop the running Codex job.\n"
            "/report - Resend the latest report."
        )

    def status_text(self) -> str:
        with self.lock:
            current = self.current_job
            last = self.last_job
        if current:
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
                f"Project: {current.project_key}\n"
                f"Started: {current.started_at}\n"
                f"Runtime: {runtime_seconds}s"
            )
        if last is None:
            return "idle"
        if last.status == "success":
            return f"last_success\nJob: {last.job_id}\nProject: {last.project_key}"
        if last.status in {"failed", "cancelled"}:
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
            return "Usage: /run <project> <prompt>\nProjects: gpt, endoflip"
        if project_key not in self.config.projects:
            return "Unknown project. Allowed projects: gpt, endoflip"

        project_dir = self.config.projects[project_key].resolve()
        if not project_dir.exists() or not project_dir.is_dir():
            return f"Project directory is not available: {project_dir}"

        with self.lock:
            if self.current_job is not None:
                return "A job is already running. Use /status or /cancel."
            job = self._create_job(chat_id, project_key, project_dir, user_prompt)
            self.current_job = job

        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()
        return (
            "🚀 Codex job started\n"
            f"Job: {job.job_id}\n"
            f"Project: {job.project_key}"
        )

    def cancel_current_job(self) -> str:
        with self.lock:
            job = self.current_job
        if job is None:
            return "No job is running."
        if job.process is None:
            return "Job is starting. Try /cancel again."
        job.status = "cancelled"
        self._terminate_process(job.process)
        return f"🛑 Codex job cancelled\nJob: {job.job_id}"

    def resend_report(self, chat_id: int) -> str:
        with self.lock:
            job = self.last_job
        if job is None:
            return "No report is available."
        if job.report_path.exists():
            self._send_document(chat_id, job.report_path, f"Report: {job.job_id}")
            return f"Report resent.\nJob: {job.job_id}"
        fallback = self._fallback_output(job)
        return f"No report.md found for {job.job_id}.\n\n{fallback}"

    def _create_job(
        self, chat_id: int, project_key: str, project_dir: Path, user_prompt: str
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

        codex_prompt = REPORT_INSTRUCTIONS.format(user_prompt=user_prompt)
        prompt_path = job_dir / "prompt.txt"
        codex_prompt_path = job_dir / "codex_prompt.txt"
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        report_path = job_dir / "report.md"
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
            metadata_path=metadata_path,
            started_at=utc_now_iso(),
            chat_id=chat_id,
        )
        job.save_metadata()
        return job

    def _run_job(self, job: Job) -> None:
        command = [
            self.config.codex_command,
            "exec",
            "--cd",
            str(job.project_dir),
            "--sandbox",
            self.config.default_sandbox,
            "-o",
            str(job.report_path),
            job.codex_prompt_path.read_text(encoding="utf-8"),
        ]

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

        with self.lock:
            self.current_job = None
            self.last_job = job

        if job.chat_id is not None and job.status != "cancelled":
            self._notify_job_finished(job)

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
                self._send_document(job.chat_id, job.report_path, "Codex report")
            else:
                self._send(job.chat_id, self._fallback_output(job))
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
            self._send_document(job.chat_id, job.report_path, "Codex report")

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
            return Job(
                job_id=metadata.get("job_id", job_dir.name),
                project_key=metadata.get("project_key", ""),
                project_dir=Path(metadata.get("project_dir", "")),
                job_dir=job_dir,
                prompt_path=job_dir / "prompt.txt",
                codex_prompt_path=job_dir / "codex_prompt.txt",
                stdout_path=job_dir / "stdout.log",
                stderr_path=job_dir / "stderr.log",
                report_path=job_dir / "report.md",
                metadata_path=metadata_path,
                started_at=metadata.get("started_at", ""),
                status=metadata.get("status", "failed"),
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
    config = Config(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=123,
        codex_command="codex",
        default_sandbox="workspace-write",
        projects={
            "gpt": PROJECT_ROOT,
            "endoflip": PROJECT_ROOT,
        },
        jobs_dir=APP_DIR / "jobs",
        poll_interval_seconds=2,
    )
    controller = CodexController(config, telegram=None)
    help_response = controller.handle_message(
        {"chat": {"id": 1}, "from": {"id": 123}, "text": "/help"}
    )
    unauthorized = controller.handle_message(
        {"chat": {"id": 1}, "from": {"id": 999}, "text": "/run gpt test"}
    )
    status = controller.handle_message(
        {"chat": {"id": 1}, "from": {"id": 123}, "text": "/status"}
    )
    assert help_response and "/run <project> <prompt>" in help_response
    assert unauthorized == "unauthorized"
    assert status in {"idle"} or status.startswith(("last_success", "last_failed"))
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
