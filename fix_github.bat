@echo off
chcp 936 >nul
title GitHub 访问修复
setlocal

if /i "%FIXGH_NOELEV%"=="1" goto :run

fltmc >nul 2>&1
if errorlevel 1 (
  echo [提示] 需要管理员权限, 正在弹出授权窗口, 请点"是".
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

:run
if not defined FIXGH_HOSTS set "FIXGH_HOSTS=%SystemRoot%\System32\drivers\etc\hosts"
if not exist "%FIXGH_HOSTS%" (
  echo [错误] 找不到 hosts 文件: %FIXGH_HOSTS%
  pause
  exit /b 1
)

echo [1/4] 备份 hosts 到 hosts.bak
copy /y "%FIXGH_HOSTS%" "%FIXGH_HOSTS%.bak" >nul
if errorlevel 1 (
  echo [错误] 备份失败, 已中止, 什么都没改.
  pause
  exit /b 1
)

echo [2/4] 清理上一次的修复记录
powershell -NoProfile -Command "$p=$env:FIXGH_HOSTS; $l=Get-Content -LiteralPath $p | Where-Object { $_ -notmatch 'study-platform-fix' }; Set-Content -LiteralPath $p -Value $l -Encoding ASCII"

echo [3/4] 写入 github.com 固定 IP
>>"%FIXGH_HOSTS%" echo 140.82.113.3 github.com # study-platform-fix

echo [4/4] 刷新 DNS 缓存
ipconfig /flushdns >nul

echo.
echo 完成. 关掉报错的那个标签页, 重新打开 https://github.com
echo.
echo 撤销办法: 记事本打开 %FIXGH_HOSTS% , 删掉带 study-platform-fix 的那一行, 保存.
echo.
pause
