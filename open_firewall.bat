@echo off
chcp 936 >nul
REM Add Windows Firewall inbound rule for port 8501 (LAN access)
net session >nul 2>&1
if errorlevel 1 (
    echo ========================================
    echo   请右键此文件, 选择 以管理员身份运行
    echo ========================================
    pause
    exit /b 1
)

netsh advfirewall firewall add rule name="StudyPlatform-8501" dir=in action=allow protocol=TCP localport=8501 profile=any

echo.
echo ========================================
echo   防火墙规则已添加
echo   孩子的手机/平板连同一 WiFi 后
echo   浏览器打开 http://192.168.3.228:8501
echo ========================================
pause
