@echo off
rem Kill only the worker tray icon (pythonw running worker-tray.py) —
rem never a blanket taskkill on pythonw, which could reap unrelated
rem processes. wmic is deprecated; powershell is present on Win10+.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter ""name='pythonw.exe'"" | Where-Object { $_.CommandLine -like '*worker-tray.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
