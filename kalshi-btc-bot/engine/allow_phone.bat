@echo off
REM Lets your phone reach the engine: opens port 8777 in Windows Firewall (all network types) and marks your Wi-Fi as Private.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
netsh advfirewall firewall delete rule name="Kalshi Engine" >nul 2>&1
netsh advfirewall firewall add rule name="Kalshi Engine" dir=in action=allow protocol=TCP localport=8777 profile=any
powershell -NoProfile -Command "Get-NetConnectionProfile | Where-Object {$_.NetworkCategory -eq 'Public'} | Set-NetConnectionProfile -NetworkCategory Private" >nul 2>&1
echo.
echo Done. Your phone can now reach the engine on port 8777.
echo If it still fails: turn off any VPN on the phone, and make sure both are on the same Wi-Fi (not a guest network).
pause
