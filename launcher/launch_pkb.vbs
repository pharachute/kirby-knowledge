' Personal Knowledge Base 1.0 - windowless launcher (ASCII only: VBScript is read as ANSI).
' Double-clicking the desktop shortcut runs this file through wscript.exe.
' It only starts the Python launcher; that launcher waits for /healthz and opens the browser.
Option Explicit

Dim shell, fso, root, script, candidates, i, pythonw, command, missing

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' this file lives in <root>\launcher\launch_pkb.vbs
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
script = fso.BuildPath(root, "launcher\launch_pkb.py")

candidates = Array("D:\python\pythonw.exe", "pythonw.exe", "python.exe")
pythonw = ""
For i = 0 To UBound(candidates)
    If pythonw = "" Then
        If InStr(candidates(i), "\") > 0 Then
            If fso.FileExists(candidates(i)) Then pythonw = candidates(i)
        Else
            pythonw = candidates(i)
        End If
    End If
Next

If pythonw = "" Then
    MsgBox "Personal Knowledge Base could not start: pythonw.exe was not found." & vbCrLf & _
           "Install Python or add it to PATH, then try again.", 16, "Personal Knowledge Base"
    WScript.Quit 1
End If

command = """" & pythonw & """ """ & script & """"
shell.CurrentDirectory = root
shell.Run command, 0, False
