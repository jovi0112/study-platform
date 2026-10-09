@echo off
chcp 936 >nul
REM 一键打包 study-platform 为 zip
setlocal
set OUT=study-platform-portable.zip
cd /d "%~dp0"

powershell -NoProfile -Command "Compress-Archive -Path app.py,db.py,ocr.py,llm.py,web_search.py,requirements.txt,install.bat,start.bat,open_firewall.bat,README.md,data,uploads -DestinationPath '%OUT%' -Force"

if exist "%OUT%" (
    echo.
    echo ========================================
    echo   已生成 %OUT%
    echo   把这个 zip 拷到目标电脑解压, 双击 install.bat
    echo ========================================
) else (
    echo [错误] 打包失败
)
pause
endlocal
