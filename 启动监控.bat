@echo off
REM GUI launch: no console window. Uses pythonw (silent) with the D:\apps\Python interpreter.
cd /d "%~dp0"
start "" "D:\apps\Python\pythonw.exe" "main.py"
exit
