param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('parking', 'passengers')]
    [string]$Mode,

    [ValidateSet(0, 1)]
    [int]$DayOffset = 0,

    [ValidateSet('baseline', 'recheck')]
    [string]$Phase = 'recheck'
)

$ErrorActionPreference = 'Stop'
$cutoff = [datetime]::new(2026, 10, 8, 23, 59, 59)
if ((Get-Date) -gt $cutoff) { exit 0 }

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$collector = Join-Path $projectRoot '.venv\Scripts\airport-parking.exe'
$logDirectory = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logPath = Join-Path $logDirectory ('{0}-{1}.log' -f $Mode, (Get-Date -Format 'yyyyMMdd'))

if (-not (Test-Path -LiteralPath $collector)) {
    "$(Get-Date -Format o) ERROR: collector executable not found: $collector" | Out-File -LiteralPath $logPath -Append -Encoding utf8
    exit 1
}

Set-Location -LiteralPath $projectRoot
if ($Mode -eq 'parking') {
    $commandArgs = @('collect')
} else {
    $commandArgs = @('collect-passengers', '--day-offset', "$DayOffset", '--phase', $Phase)
}

"$(Get-Date -Format o) START $Mode $($commandArgs -join ' ')" | Out-File -LiteralPath $logPath -Append -Encoding utf8
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $collector @commandArgs 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding utf8
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
"$(Get-Date -Format o) END exit=$exitCode" | Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $exitCode
