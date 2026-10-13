[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$PythonExe,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$PipelineArgs
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $PythonExe = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Pass -PythonExe with Python 3.12+ and install requirements-color.txt in that environment.'
}
& $PythonExe (Join-Path $PSScriptRoot 'balance_sky.py') @PipelineArgs
exit $LASTEXITCODE
