@echo off
rem Pre-install stop — runs BEFORE payload extraction. A running
rem worker task, tray pythonw, or reconfigure console keeps
rem python-embed DLLs locked and the overwrite fails mid-install
rem ('_bz2.pyd — error opening it'). schtasks /End does not always
rem reap task children, so kill every process whose executable lives
rem under this install dir — path-scoped, unrelated processes are
rem never touched.
schtasks /End /TN "TestOrchestratorBuildbotWorker" >nul 2>&1
powershell -NoProfile -Command "Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith('%~dp0', 'CurrentCultureIgnoreCase') } | Stop-Process -Force -ErrorAction SilentlyContinue"
