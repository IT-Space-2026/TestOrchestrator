@echo off
rem Relaunch the worker tray icon (pythonw worker-tray.py) after it
rem was stopped via stop-tray.cmd or Quit. %~dp0 = this file's dir =
rem the install dir, so it works regardless of CWD.
start "" "%~dp0python-embed\pythonw.exe" "%~dp0worker-tray.py"
