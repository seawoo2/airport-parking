<# Register 17:10 sync, 17:15 next-day prediction and 00:10 completed-day evaluation. #>
param([string]$ConfigPath)

$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'Korea Standard Time') { throw 'Set Windows timezone to Korea Standard Time.' }
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $projectRoot 'local-work\server-sync.json' }
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$syncConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run uv sync first.' }
$syncRunner = Join-Path $PSScriptRoot 'sync-server-data.py'
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$jobs = @(
    @{ Name = 'AirportParking-ServerSync-1710'; At = '17:10'; Args = ('"{0}" --config "{1}"' -f $syncRunner, $ConfigPath); Description = 'Incremental server sync at 17:10 KST.' },
    @{ Name = 'AirportParking-NextDayPrediction'; At = '17:15'; Args = ('-m airport_parking.daily_analysis predict --config "{0}" --data-root "{1}"' -f $ConfigPath, $syncConfig.output_dir); Description = 'Predict next-day hourly mean/max/min occupancy; refresh server data first.' },
    @{ Name = 'AirportParking-NextDayEvaluation'; At = '00:10'; Args = ('-m airport_parking.daily_analysis evaluate --config "{0}" --data-root "{1}"' -f $ConfigPath, $syncConfig.output_dir); Description = 'Evaluate completed prior-day forecasts using observed hourly mean/max/min occupancy.' }
)
foreach ($job in $jobs) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $job.Args -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $job.At
    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $job.Description -Force | Out-Null
    $info = Get-ScheduledTaskInfo -TaskName $job.Name
    [pscustomobject]@{ TaskName = $job.Name; NextRunTime = $info.NextRunTime; LastTaskResult = $info.LastTaskResult }
}
