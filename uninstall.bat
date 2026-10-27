@echo off
setlocal
cd /d "%~dp0"
title Uninstall Mousette

rem Safety check: only ever delete a folder that really is Mousette.
if not exist "server\__main__.py" (
    echo This doesn't look like the Mousette folder. Nothing was removed.
    pause
    exit /b 1
)

rem Admin rights are needed to remove Mousette's Windows Firewall rule.
net session >nul 2>nul && goto :elevated
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" && exit /b 0
echo Could not get administrator rights: the firewall rule will be left in place.

:elevated
set "PM_DIR=%~dp0"
set "PM_DIR=%PM_DIR:~0,-1%"
echo This removes Mousette from this PC:
echo   - stops Mousette if it is running
echo   - removes the "Mousette" Windows Firewall rule, if you added one
echo   - deletes the folder %PM_DIR%
echo     (including the .venv Python environment)
echo.
choice /c YN /m "Uninstall Mousette"
if errorlevel 2 (echo Cancelled. Nothing was removed. & pause & exit /b 0)

rem Stop Mousette: python.exe in .venv, and the Python it starts (which lives elsewhere).
rem Then remove the firewall rule from the README and rules for programs in this folder.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = $env:PM_DIR + '\';" ^
  "$procs = @(Get-CimInstance Win32_Process);" ^
  "$ours = @($procs | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir, 'OrdinalIgnoreCase') });" ^
  "$ours += $procs | Where-Object { $ours.ProcessId -contains $_.ParentProcessId };" ^
  "$ours | ForEach-Object { Write-Host ('Stopping ' + $_.Name + ' (' + $_.ProcessId + ')'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };" ^
  "try {" ^
  "  $rules = @(Get-NetFirewallRule -DisplayName 'Mousette','CouchPad','PhoneMouse' -ErrorAction SilentlyContinue);" ^
  "  $rules += Get-NetFirewallApplicationFilter -ErrorAction Stop | Where-Object { $_.Program -and $_.Program.StartsWith($dir, 'OrdinalIgnoreCase') } | Get-NetFirewallRule;" ^
  "  $rules | Where-Object { $_ } | ForEach-Object { Write-Host ('Removing firewall rule: ' + $_.DisplayName); Remove-NetFirewallRule -Name $_.Name -ErrorAction Stop }" ^
  "} catch { Write-Host 'Could not remove the firewall rule (needs administrator rights).' }"

echo.
echo Mousette is uninstalled. When you close this window, the folder is deleted.
echo On the iPhone/iPad, press and hold the Mousette icon and choose Remove App.
pause

rem The folder can't be deleted while this script runs from it, so a small script in
rem TEMP waits for this window to close, deletes the folder, then deletes itself.
set "CLEANUP=%TEMP%\mousette-uninstall.bat"
> "%CLEANUP%" echo @echo off
>> "%CLEANUP%" echo timeout /t 2 /nobreak ^>nul
>> "%CLEANUP%" echo rd /s /q "%PM_DIR%"
>> "%CLEANUP%" echo (goto) 2^>nul ^& del "%%~f0"
cd /d "%TEMP%"
start "" /min "%CLEANUP%"
exit
