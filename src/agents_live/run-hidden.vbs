' run-hidden.vbs — Run a command with no visible console window.
' Used by Windows Task Scheduler to launch wsl.exe silently.
'
' Usage: wscript.exe run-hidden.vbs "command with arguments"
'
' The first argument is the full command line to execute.
' Window style 0 = hidden, second parameter True = wait for completion.

If WScript.Arguments.Count < 1 Then
    WScript.Echo "Usage: wscript.exe run-hidden.vbs ""command args..."""
    WScript.Quit 1
End If

CreateObject("WScript.Shell").Run WScript.Arguments(0), 0, True
