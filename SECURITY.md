# Security Policy

## Local Privacy & Data Protection

This project is designed around strict **local-only, read-only** processing:
1. Original chat databases (QQ / WeChat) are copied to a local sandbox before decryption and are never modified in place.
2. Database keys, app secrets, decrypted mirrors, and extracted message materials **must never be committed or uploaded** to public repositories.

### Files That Must Stay Local (Never Commit)
- `.env` and any `.env.*` (except `.env.example`)
- `work/` (decrypted mirrors in `work/wx_mirror/`, keyring `work/wx_keys.json`, logs, message history, inbox files)
- `output/` (extracted todo lists and diagnostic reports)
- `config/schema.json`, `config/active_platforms.json`, `config/wizard_state.json`
- `vendor/wechat-key/key_windows.txt` or any `*.key` / `*.db` files
- `automation-prompt.local.md`

## Reporting a Vulnerability

If you discover a security issue (such as credential leakage, path traversal, or unintended data exposure):
- **Do not** open a public issue containing real keys, database files, or private chat logs.
- Report the issue privately via GitHub Security Advisories ("Report a vulnerability" in the repository Security tab) or describe the minimal reproducible steps without any sensitive data.
