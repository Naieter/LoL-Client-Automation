Set oShell = CreateObject("WScript.Shell")
Dim scriptDir
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
oShell.CurrentDirectory = scriptDir
oShell.Run "python lol_tool.py", 1, False
