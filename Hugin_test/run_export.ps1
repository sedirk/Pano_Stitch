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
    throw 'Pass -PythonExe with Python and the dependencies in requirements-color.txt.'
}
& $PythonExe (Join-Path $PSScriptRoot 'export_jpg.py') @PipelineArgs
exit $LASTEXITCODE
