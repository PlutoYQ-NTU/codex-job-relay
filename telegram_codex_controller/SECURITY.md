# Security Policy

## Sensitive Credentials

Telegram bot tokens are highly sensitive credentials. Anyone with the token can
control the bot until the token is revoked or rotated.

Do not commit:

- `telegram_codex_controller/.env`
- Telegram bot tokens
- OpenAI API keys
- GitHub tokens
- cookies, sessions, workspace tokens, or other secrets

Telegram normal bot chats are not end-to-end encrypted. Do not send passwords,
API keys, private tokens, or other credentials to the bot.

## Recommended Use

- Use a private 1:1 chat with the bot.
- Configure exactly one trusted `TELEGRAM_ALLOWED_USER_ID`.
- Do not expose the bot to groups or multiple users unless you have reviewed the
  risk.
- Keep the controller on a trusted local machine.
- Keep job directories local and private.

## Job Logs And Artifacts

The `jobs/` directory may contain logs, local paths, report text, model output,
or artifact names. It is ignored by default except for `jobs/.gitkeep`.

Review job artifacts before sharing them publicly.

## Execution Boundaries

This project does not support arbitrary shell execution from Telegram.

The controller uses whitelist project paths, whitelist local jobs, task-level
approval, and `subprocess.Popen([...], shell=False)`.

Write and long-running jobs require approval. Dirty repositories become
`blocked_repo_dirty` unless the user explicitly sends `/approve_anyway`.

## Reporting Vulnerabilities

If you find a security issue, please open a private report to the maintainer if
available, or create a GitHub issue that describes the impact without including
tokens, secrets, private logs, or exploit payloads.

