$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName "SchoolAssistant" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "SchoolAssistant" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "SchoolAssistant" -Confirm:$false
    Write-Host "已移除 SchoolAssistant 计划任务。"
} else {
    Write-Host "未发现 SchoolAssistant 计划任务。"
}
