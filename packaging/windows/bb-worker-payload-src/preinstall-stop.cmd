@echo off
rem Pre-install stop — runs BEFORE payload extraction. A running
rem worker task child, tray pythonw, or reconfigure console keeps
rem python-embed DLLs locked and the overwrite fails mid-install
rem ('_bz2.pyd - error opening it').
rem
rem NOTE: the NSIS stub is 32-bit, so this cmd and powershell run
rem 32-bit — 32-bit Get-Process CANNOT read .Path of 64-bit
rem processes (returns $null), which silently matched nothing.
rem Fixes below:
rem   1) taskkill /IM by image name — works cross-bitness
rem   2) Win32_Process CIM query — the WMI service is 64-bit and
rem      resolves ExecutablePath/CommandLine for 64-bit procs
rem   3) rename python-embed aside — Windows allows renaming a dir
rem      containing loaded DLLs, so extraction gets a clean path
rem      even if a process is still shutting down
schtasks /End /TN "TestOrchestratorBuildbotWorker" >nul 2>&1

taskkill /F /IM buildbot-worker.exe >nul 2>&1
taskkill /F /IM twistd.exe >nul 2>&1

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.StartsWith('%~dp0', [StringComparison]::OrdinalIgnoreCase)) -or ($_.CommandLine -and $_.CommandLine -match 'worker-tray\.py') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

rem brief beat for handles to release
ping -n 3 127.0.0.1 >nul 2>&1

rem move any still-locked payload dir out of the extraction path
if exist "%~dp0python-embed" (
    if exist "%~dp0python-embed.old" rd /s /q "%~dp0python-embed.old" >nul 2>&1
    ren "%~dp0python-embed" "python-embed.old" >nul 2>&1
    if exist "%~dp0python-embed" ren "%~dp0python-embed" "python-embed.old.%RANDOM%" >nul 2>&1
)
exit /b 0
