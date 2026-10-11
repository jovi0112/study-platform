@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==================================================
echo   Push study-platform to GitHub (auto retry)
echo ==================================================
echo.
set /a n=0
:retry
set /a n+=1
echo [Attempt %n%] git push origin main ...
git push origin main
if %errorlevel%==0 goto ok
if %n% GEQ 15 goto fail
echo   ... failed. Retrying in 5 seconds. (Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto retry

:ok
echo.
echo   SUCCESS - your fix is now on GitHub.
echo   Next: open share.streamlit.io and Reboot your app.
goto end

:fail
echo.
echo   FAILED after %n% attempts.
echo   Check network / VPN, then double-click this file again.

:end
echo.
pause
