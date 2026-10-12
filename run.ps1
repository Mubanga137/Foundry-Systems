$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
& "$ScriptDir\.venv\Scripts\python.exe" -m app.cli $args
