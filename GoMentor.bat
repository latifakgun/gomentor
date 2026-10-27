@echo off
cd /d "%~dp0"
rem Anaconda's PySide6 has no QtMultimedia (in-app video). Use the venv if there is one.
set PY=python
if exist ".venv-build\Scripts\python.exe" set PY=.venv-build\Scripts\python.exe
%PY% ui.py --rank 15k --bot 15k --next 9k
if errorlevel 1 pause
