# Roadmap

`codex-job-relay` should stay focused on trusted local job control rather than becoming a general remote shell.

## Near Term

- Keep CI coverage on controller compilation and offline self-test.
- Improve documentation for configuration, job artifacts, and operational recovery.
- Add artifact manifest metadata for generated reports and files.
- Add heartbeat or progress notifications for long-running jobs.

## Later Ideas

- Generic `/train <alias>` or `/job <alias>` whitelist local jobs.
- YAML-defined job aliases after a careful safety review.
- HTML or PDF mobile report rendering.
- Optional image preview delivery for generated artifacts.

## Non-Goals

- Arbitrary Telegram shell commands.
- Public webhook server by default.
- Multi-user access without a reviewed authorization model.
- Bypassing Codex or local sandbox approvals.
