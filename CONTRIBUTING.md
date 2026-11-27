# Contributing Guide

Thank you for helping improve **待办自动汇总助手 (Chat To-Do Assistant)**!

## Channel Verification Status & Testing Help Wanted

- **Verified on Windows**: QQ (NT), Personal WeChat (4.x), WeCom Webhook / Inbox
- **Experimental / Untested**: **Feishu (飞书)** and **DingTalk (钉钉)** adapters have not yet undergone full real-device verification. Community testing, bug reports, and PRs for Feishu Open API and DingTalk archive/callback workflows are especially welcome!

## Local Development & Running Tests

1. Clone the repository and copy `.env.example` to `.env` if needed (do **not** commit `.env`).
2. Prepare a Python 3.10–3.13 (x64) environment (or run `获取Python.bat` on Windows to provision `.venv/` offline from `vendor/`).
3. Run the automated test suites before submitting any changes:

```bash
python tests/test_bugfixes.py
python tools/wx_selftest.py --no-scan
```

4. Syntax-check all Python files:

```bash
python -m compileall -q app.py run.py server.py tools tests
```

## Pull Request Checklist

- Ensure no `.env`, `work/`, `output/`, `config/schema.json`, or `*.key` files are included in your diff.
- Preserve existing behavior for read-only database access and explicit error reporting (`OK` / `STALE` / `PARTIAL` / `FAIL` / `NOT_READY`).
- Update `tests/test_bugfixes.py` when fixing bugs or changing API/frontend contracts.
