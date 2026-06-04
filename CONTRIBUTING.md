# Contributing

`codex-job-relay` is a local, trusted-machine controller. Changes should preserve the narrow execution model and be easy to review.

## Local Checks

```powershell
py -m py_compile telegram_codex_controller\controller.py
py telegram_codex_controller\controller.py --self-test
```

The self-test is offline and must not require real Telegram credentials.

## Safety Rules

- Do not add arbitrary shell execution from Telegram.
- Keep project selection allowlist-based.
- Keep local process launches as explicit argument lists with `shell=False`.
- Do not commit `.env`, tokens, user ids, private logs, or job artifacts.
- Do not bypass approval, dirty-repo blocking, or `/approve_anyway` semantics.

## Documentation

Update `README.md`, `SECURITY.md`, or `ROADMAP.md` when changing commands, configuration, approval behavior, or artifact handling.
