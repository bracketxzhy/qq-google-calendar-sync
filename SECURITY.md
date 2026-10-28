# Privacy and security

Do not publish real messages, screenshots containing personal information, account-selection files, tokens, database dumps, runtime logs, Google event IDs, or authorization evidence in issues, commits, CI artifacts, or support requests.

The public folder is created from an explicit source-file allowlist. A Git ignore rule is an additional barrier, not protection for files already committed. `python scripts/audit-public.py` inspects exact index blobs and emits only file locations and rule names. Review staged changes and commit identity before every publication. Use synthetic, minimized reproductions for bug reports.

Local HTTP listeners must stay on 127.0.0.1. The receiver checks HMAC signatures and account identity; it never sends QQ quick operations. Do not expose its ports through a public proxy. Installation tokens are randomly generated locally. Anyone with access to the local account/files may still access stored messages, so protect the Windows account and disk.

SnowLuma's own caching is broader than this receiver's allowlist filtering. Review the upstream privacy policy before opting in. Codex analysis processes selected messages through its service; calendar events contain concise fields and a stable identifier. Do not treat this pipeline as a local-only AI system or as a guarantee of complete message delivery.

If a secret was ever pushed, revoke/rotate it and address Git history and existing clones. Deleting a current file or adding .gitignore is insufficient. New credentials must never be pasted into a public issue or commit.
