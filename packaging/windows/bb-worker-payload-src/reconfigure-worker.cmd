@echo off
rem Reconfigure console — launched from the tray 'Configure…' item.
rem Console python (not pythonw) so the prompts are visible; pause
rem keeps results on screen when opened directly.
"%~dp0python-embed\python.exe" "%~dp0reconfigure-worker.py"
pause
