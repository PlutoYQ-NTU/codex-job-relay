# Telegram Codex Controller

This is a local Windows controller that receives Telegram Bot commands with long polling, runs Codex CLI jobs in approved project directories, and sends the job result back to Telegram.

It does not control the Codex App GUI. It does not simulate keyboard or mouse input, read windows, open a webhook, or expose a public HTTP server. The controller calls `codex exec` directly so the Codex App can still be used normally when you are at the computer.

## Files

```text
telegram_codex_controller/
  controller.py
  config.example.env
  requirements.txt
  README.md
  .gitignore
  jobs/
    .gitkeep
```

## Install

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r telegram_codex_controller\requirements.txt
copy telegram_codex_controller\config.example.env telegram_codex_controller\.env
```

Edit `telegram_codex_controller\.env` and fill in your Telegram token, allowed user id, and project paths.

## Create A Telegram Bot

1. Open Telegram and chat with `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the bot token into `TELEGRAM_BOT_TOKEN`.
4. Get your numeric Telegram user id, for example from `@userinfobot`, and put it in `TELEGRAM_ALLOWED_USER_ID`.

Only this user id can run commands.

## Configuration

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=

CODEX_COMMAND=codex
DEFAULT_SANDBOX=workspace-write

PROJECT_GPT=D:\GPT_Development
PROJECT_ENDOFLIP=D:\EndoFLIP

JOBS_DIR=telegram_codex_controller\jobs
POLL_INTERVAL_SECONDS=2
```

Allowed project keys:

```text
gpt       -> PROJECT_GPT
endoflip  -> PROJECT_ENDOFLIP
```

Telegram users cannot pass arbitrary paths. `DEFAULT_SANDBOX=danger-full-access` is rejected by the controller.

## Run

```powershell
cd <project>
.\.venv\Scripts\activate
python telegram_codex_controller\controller.py
```

The controller uses Telegram long polling. It does not use webhooks and does not listen on a local or public HTTP port.

## Commands

```text
/help
```

Show command help.

```text
/run <project> <prompt>
```

Start a Codex job in a whitelisted project.

Examples:

```text
/run gpt 检查最新训练日志，生成一份是否可以启动 v0.5 的判断报告。不要改代码。
```

```text
/run endoflip 检查 figures notebook 的路径配置，找出可能导致图片无法生成的问题，并生成报告。
```

```text
/status
```

Return `idle`, `running`, `last_success`, or `last_failed`. While running, it includes job id, project, start time, and runtime.

```text
/cancel
```

Terminate the current Codex subprocess.

```text
/report
```

Resend the latest `report.md`.

## Codex CLI Command

The intended command is:

```powershell
codex exec --cd <project_dir> --sandbox workspace-write -o <report_path> "<prompt>"
```

In this environment, these checks were attempted before implementation:

```powershell
codex --help
codex exec --help
```

Both failed with Windows `Access is denied` for:

```text
C:\Program Files\WindowsApps\OpenAI.Codex_26.519.5221.0_x64__2p2nqsd0c76g0\app\resources\codex.exe
```

`Get-Command -All codex` reported Codex version `0.133.0-alpha.1`. Because help output could not be read from this shell, the MVP uses the requested command shape. If your installed CLI exposes different flags, update `controller.py` in `_run_job()` or set `CODEX_COMMAND` to a wrapper script that accepts this shape.

Every Codex prompt is wrapped with a required Markdown report format:

```text
1. Task Summary
2. Actions Taken
3. Files Read
4. Files Modified
5. Commands Run
6. Results
7. Errors or Warnings
8. Suggested Next Steps
```

## Job Files

Each run creates:

```text
jobs/job_YYYYMMDD_HHMMSS/
  prompt.txt
  codex_prompt.txt
  stdout.log
  stderr.log
  report.md
  metadata.json
```

`metadata.json` records job id, project key, project directory, start/end time, status, and return code.

## Security Boundaries

- Only `TELEGRAM_ALLOWED_USER_ID` can run commands.
- Unauthorized users receive `unauthorized`.
- Project paths are fixed by `.env` and selected only by `gpt` or `endoflip`.
- Telegram text is never concatenated into a shell command.
- `subprocess.Popen` uses a list of arguments and `shell=False`.
- The controller does not require administrator permissions.
- Default sandbox is `workspace-write`.
- `danger-full-access` is rejected.
- No webhook, HTTP server, or public port is opened.
- Job logs stay under `telegram_codex_controller\jobs`.
- Only one job runs at a time.

## Troubleshooting

If the controller exits on startup, check that `telegram_codex_controller\.env` exists and contains `TELEGRAM_BOT_TOKEN` and numeric `TELEGRAM_ALLOWED_USER_ID`.

If `/run` says the project directory is not available, confirm `PROJECT_GPT` or `PROJECT_ENDOFLIP` exists on this Windows machine.

If a Codex job fails immediately, inspect the job's `stderr.log`. Also run this manually in PowerShell:

```powershell
codex --help
codex exec --help
```

If PowerShell reports `Access is denied`, fix the local Codex CLI installation or set `CODEX_COMMAND` to a working Codex CLI executable or wrapper.

If Telegram messages are not received, verify the bot token, make sure no other process is polling the same bot, and confirm the machine has internet access.

## Offline Verification

Without a Telegram token, you can run:

```powershell
python telegram_codex_controller\controller.py --self-test
```

This checks `/help`, unauthorized user handling, and idle `/status` without network access.
