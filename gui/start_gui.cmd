@echo off
setlocal
set "SERVO_PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%SERVO_PY%" set "SERVO_PY=python.exe"
"%SERVO_PY%" "%~dp0servo_gui.py"
if errorlevel 1 pause
