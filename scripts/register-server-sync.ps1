<# Register daily and logon downloads from the server for local analysis. #>
param(
    [Parameter(Mandatory = $true)][string]$ServerHost,
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [string]$RemoteDirectory = '/home/ubuntu/airport-parking',
    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At = '09:00'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'sync-server-data.py'
$resolvedKey = (Resolve-Path -LiteralPath $KeyPath).Path
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run uv sync first.' }
if ($ServerHost -notmatch '^[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*$') {
    throw 'ServerHost must have the form ubuntu@IP-or-DNS-name.'
}
if (-not $RemoteDirectory.StartsWith('/')) { throw 'RemoteDirectory must be absolute.' }
if ((Get-TimeZone).Id -ne 'Korea Standard Time') { throw 'Set the Windows timezone to Korea Standard Time first.' }

$configDirectory = Join-Path $projectRoot 'local-work'
New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
$configPath = Join-Path $configDirectory 'server-sync.json'
@{
    host = $ServerHost
    key_path = $resolvedKey
    remote_dir = $RemoteDirectory
    output_dir = Join-Path $projectRoot 'data\raw\server'
} | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8

$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}" --config "{1}"' -f $scriptPath, $configPath) -WorkingDirectory $projectRoot
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At $At
    New-ScheduledTaskTrigger -AtLogOn -User $account
)
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'AirportParking-ServerSync' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Download new server rows into the cumulative local analysis store.' -Force | Out-Null
Get-ScheduledTaskInfo -TaskName 'AirportParking-ServerSync' | Select-Object LastRunTime, NextRunTime, LastTaskResult
