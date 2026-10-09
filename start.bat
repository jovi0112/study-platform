@echo off
chcp 936 >nul
REM 学习积累平台 - 一键启动 (Windows)
setlocal

set PY=C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe
set APPDIR=%~dp0
cd /d %APPDIR%

REM 可选: 在此设置 LLM 环境变量(接大模型时启用)
REM set STUDY_LLM_PROVIDER=deepseek
REM set STUDY_LLM_API_KEY=sk-xxx
REM set STUDY_LLM_MODEL=deepseek-chat

echo ========================================
echo   学习积累平台 - 启动中
echo   本机访问:   http://localhost:8501
echo   手机访问:   http://192.168.3.228:8501
echo   (手机需连同一 WiFi, 且已运行过 open_firewall.bat)
echo   按 Ctrl+C 退出
echo ========================================

"%PY%" -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501

endlocal
