' Hidden launcher for the "toggle production build" shortcut.
' cmd /c with a hidden window keeps the PowerShell console from flashing.
Option Explicit
Dim sh, fso, base
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Dim args, action
Set args = WScript.Arguments
action = "toggle"
If args.Count > 0 Then action = args(0)
sh.Run "cmd /c """ & base & "\switch.bat"" " & action, 0, False
