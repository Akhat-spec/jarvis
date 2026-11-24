@echo off
rem J.A.R.V.I.S. launcher (no console window, log: logs\jarvis.log)
cd /d "%~dp0"
start "" pythonw run.py
