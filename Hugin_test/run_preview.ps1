[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$PythonExe,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$PipelineArgs
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $panoCandidates = @(
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
        'D:\ProgramData\anaconda3\python.exe',
        (Join-Path $env:USERPROFILE 'anaconda3\python.exe')
    )
    $panoCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($panoCommand -and $panoCommand.Source -notlike '*\WindowsApps\*') {
        $panoCandidates = $panoCandidates + @($panoCommand.Source)
    }
    $PythonExe = $panoCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $PythonExe) { throw 'Python not found. Pass -PythonExe with a Python 3.10+ executable.' }
& $PythonExe (Join-Path $PSScriptRoot 'test.py') --optimize-hfov --width 4096 @PipelineArgs
exit $LASTEXITCODE
