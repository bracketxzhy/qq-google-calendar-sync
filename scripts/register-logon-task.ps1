param([switch]$Enable, [string]$PythonExe)
$ErrorActionPreference = 'Stop'
if (-not $Enable) { throw 'Pass -Enable to explicitly opt in to one bounded attach at logon.' }
$projectPath = Split-Path -Parent $PSScriptRoot
if (-not $PythonExe) { throw 'Pass the absolute python.exe path used during setup.' }
$resolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$pythonWindowless = Join-Path (Split-Path -Parent $resolvedPython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw 'Matching pythonw.exe not found.' }
$policyPath = Join-Path $projectPath '.private\snowluma-startup.json'
$policy = Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskSuffix = & $resolvedPython -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' $projectPath
if ($LASTEXITCODE -ne 0) { throw 'Could not identify this installation.' }
$taskName = "QQ Calendar Logon $taskSuffix"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect it instead of overwriting.' }
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('"' + (Join-Path $PSScriptRoot 'start-snowluma-on-logon.pyw') + '"') -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$trigger.Delay = 'PT20S'
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$options = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
$policy.enabled = $true
$policy.authorization = 'explicit_local_setup'
[IO.File]::WriteAllText($policyPath, ($policy | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $options | Out-Null
} catch {
    $policy.enabled = $false
    [IO.File]::WriteAllText($policyPath, ($policy | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
    throw
}
Write-Output "Registered $taskName. Runs at next logon; does not start now, launch QQ, or retry after failure."
