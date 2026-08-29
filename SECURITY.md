# Security and privacy

## Scope

Use this project only with your own Windows account, your own Weixin account,
and data you are authorized to process. It reads sensitive local process memory
and chat databases. Do not deploy it on another person's machine or account.

## Secrets

- Put the DeepSeek key only in `config/.env.school`.
- That file is ignored by Git. Never paste a real key into source, issues,
  screenshots, logs, or example files.
- If a key was ever committed, revoke it at the provider immediately; deleting
  the latest commit is not sufficient because Git history retains it.

Before publishing, inspect staged files:

```powershell
git status --short
git diff --cached
rg -n --hidden -g '!data/**' -g '!logs/**' -g '!.venv/**' "sk-[A-Za-z0-9_-]{16,}"
```

## Private data

`data/` may contain message text, attachment text, group identifiers, tasks and
backups. `logs/` may contain operational errors. Both directories are ignored
and must never be uploaded.

Only groups explicitly selected in the local website are analyzed. Their
selected messages and locally extracted attachment text are sent to the
configured DeepSeek API. Nothing is sent to the project's authors.

## Network exposure

The web service binds to `127.0.0.1` by default. Do not change it to `0.0.0.0`
unless you add authentication and understand the privacy impact.

## Reporting

Please report security issues privately to the repository maintainer rather
than opening a public issue containing keys, chat records or database samples.
