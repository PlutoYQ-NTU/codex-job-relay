# Telegram Controller Module

This directory contains the Windows Telegram long-polling controller used by
`codex-job-relay`.

For the project overview, install guide, command list, security model, and
roadmap, see the repository root `README.md`.

## Files

```text
telegram_codex_controller/
  controller.py
  config.example.env
  requirements.txt
  README.md
  SECURITY.md
  LICENSE
  .gitignore
  jobs/
    .gitkeep
```

## Runtime Files

Create `telegram_codex_controller/.env` from `config.example.env` and keep it
private. The local `.gitignore` ignores `.env`, logs, Python cache files, local
virtual environments, and job output under `jobs/`.

Each job creates a directory under `jobs/` with prompts, metadata, logs, and
reports. `report_telegram.md` is generated with UTF-8 BOM for better mobile
display in Telegram clients.

## Local Verification

From the repository root:

```powershell
py telegram_codex_controller\controller.py --self-test
py -m py_compile telegram_codex_controller\controller.py
```

