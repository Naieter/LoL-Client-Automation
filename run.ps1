$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir
python lol_tool.py
