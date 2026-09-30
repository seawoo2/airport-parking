<#
Register finite local collection tasks through 2026-10-08 (Korea time).
Run once in PowerShell after uv sync and Docker PostgreSQL setup.
#>

$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'Korea Standard Time') {
    throw 'Windows time zone must be Korea Standard Time for these local tasks.'
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run-scheduled-collector.ps1'
$powerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$now = Get-Date
$cutoff = [datetime]::new(2026, 10, 8, 23, 59, 59)
if ($now -ge $cutoff) { throw 'The collection end date has passed.' }

function Register-CollectorTask {
    param([string]$Name, [string]$Arguments, [object[]]$Triggers)
    if ($Triggers.Count -eq 0) { return }
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" {1}' -f $runner, $Arguments) -WorkingDirectory $projectRoot
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers -Principal $principal -Settings $settings -Description 'Airport parking local data collection; ends 2026-10-08 KST.' -Force | Out-Null
    $task = Get-ScheduledTask -TaskName $Name
    $info = Get-ScheduledTaskInfo -TaskName $Name
    [pscustomobject]@{ TaskName = $Name; Triggers = $task.Triggers.Count; NextRunTime = $info.NextRunTime; State = $task.State }
}

$firstParking = $now.Date.AddMinutes(([math]::Floor($now.TimeOfDay.TotalMinutes / 10) + 1) * 10)
$parkingTrigger = New-ScheduledTaskTrigger -Once -At $firstParking -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration ($cutoff - $firstParking)
Register-CollectorTask -Name 'AirportParking-Parking-10min' -Arguments '-Mode parking' -Triggers @($parkingTrigger)

$forecastSchedules = @(
    @{ Name = 'AirportParking-Forecast-1105'; Hour = 11; Arguments = '-Mode passengers -DayOffset 0 -Phase recheck' },
    @{ Name = 'AirportParking-Forecast-1705'; Hour = 17; Arguments = '-Mode passengers -DayOffset 1 -Phase baseline' },
    @{ Name = 'AirportParking-Forecast-2305'; Hour = 23; Arguments = '-Mode passengers -DayOffset 1 -Phase recheck' }
)

foreach ($schedule in $forecastSchedules) {
    $triggers = @(
        for ($day = $now.Date; $day -le $cutoff.Date; $day = $day.AddDays(1)) {
            $runAt = $day.AddHours($schedule.Hour).AddMinutes(5)
            if ($runAt -gt $now -and $runAt -le $cutoff) {
                New-ScheduledTaskTrigger -Once -At $runAt
            }
        }
    )
    Register-CollectorTask -Name $schedule.Name -Arguments $schedule.Arguments -Triggers $triggers
}
