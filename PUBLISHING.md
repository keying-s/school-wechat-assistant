# 发布到 GitHub

请只发布这个整理后的目录，不要把原工作目录整体上传。

## 发布前检查

```powershell
git init -b main
git add -- .github .gitignore config integrations vendor school_assistant static tests app.py LICENSE PUBLISHING.md README.md SECURITY.md THIRD_PARTY_NOTICES.md requirements.txt setup.ps1 register_school_task.ps1 unregister_school_task.ps1 打开日程表.url 启动学校日程.bat 停止学校日程.bat
git status --short
git diff --cached --check
git diff --cached
rg -n --hidden -g '!vendor/**' "sk-[A-Za-z0-9_-]{16,}"
```

重点确认没有以下内容：

- `config/.env.school` 或其他真实 `.env`；
- `data/`、`logs/`、微信数据库或备份；
- 真实群名、聊天内容、账号标识和本机绝对路径；
- `.venv/`、`__pycache__/`、压缩包和临时文件。

## 首次推送

先在 GitHub 创建一个空仓库，不要自动生成 README 或许可证。然后在本目录执行：

```powershell
git init -b main
git add -- .github .gitignore config integrations vendor school_assistant static tests app.py LICENSE PUBLISHING.md README.md SECURITY.md THIRD_PARTY_NOTICES.md requirements.txt setup.ps1 register_school_task.ps1 unregister_school_task.ps1 打开日程表.url 启动学校日程.bat 停止学校日程.bat
git commit -m "Initial public release"
git remote add origin https://github.com/<your-name>/<your-repo>.git
git push -u origin main
```

建议先使用私有仓库核对 GitHub 文件列表，确认无误后再改为公开。仓库采用 MIT License；如果不希望允许他人自由使用、修改和再分发，请在公开前更换许可证。

## Key 泄露后的处理

如果真实 API key 曾进入任何 Git 提交，即使之后删除文件，也应立即在服务商后台撤销并创建新 key。Git 历史重写只能清理仓库记录，不能使旧 key 重新安全。
