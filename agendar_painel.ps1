# Optional: start the AI Desk Bot dashboard (http://127.0.0.1:8899) automatically at Windows logon.
# Run it yourself in PowerShell:  powershell -ExecutionPolicy Bypass -File .\agendar_painel.ps1
# Undo:                           Unregister-ScheduledTask -TaskName "AI Desk Dashboard" -Confirm:$false
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$PSScriptRoot\.venv\Scripts\pythonw.exe" -Argument "-m deskbot dashboard" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AI Desk Dashboard" -Action $action -Trigger $trigger -Settings $settings -Description "Painel do AI Desk Bot em http://127.0.0.1:8899" -Force | Out-Null
Write-Host "Tarefa 'AI Desk Dashboard' criada. Arranca no proximo login (ou agora com: Start-ScheduledTask -TaskName 'AI Desk Dashboard')."
