@echo off
chcp 936 >nul
REM 目标电脑首次运行: 一键装依赖
setlocal

set PY=python
where %PY% >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 python, 请先安装 Python 3.10+
    echo        下载: https://www.python.org/downloads/
    echo        安装时务必勾选 Add Python to PATH
    pause
    exit /b 1
)

echo ========================================
echo   学习平台 - 首次安装依赖
echo   目录: %~dp0
echo ========================================

cd /d "%~dp0"

echo [1/3] 升级 pip ...
%PY% -m pip install --upgrade pip --quiet

echo [2/3] 安装核心依赖 (streamlit pillow requests rapidocr) ...
%PY% -m pip install streamlit pillow requests rapidocr-onnxruntime --quiet
if errorlevel 1 (
    echo [错误] 依赖安装失败, 请检查网络
    pause
    exit /b 1
)

echo [3/3] 初始化数据库 ...
%PY% -c "import db; db.init_db(); print('  数据库已就绪: data/study.db')"

echo.
echo ========================================
echo   安装完成! 双击 start.bat 启动
echo   浏览器访问 http://localhost:8501
echo ========================================
pause
endlocal
