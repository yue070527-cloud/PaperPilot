@echo off
chcp 65001 >nul 2>&1
title PaperPilot

echo.
echo   ╔══════════════════════════════════════╗
echo   ║   PaperPilot 智能文献工作流系统      ║
echo   ╚══════════════════════════════════════╝
echo.

REM ── 检测 Python ──
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] 未检测到 Python
    echo       请安装 Python 3.10 或以上版本
    echo       下载地址: https://python.org
    echo       安装时请勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   [OK] Python %PYVER%

REM ── 安装依赖 ──
echo   [..] 正在检查依赖...
python -m pip install -r requirements.txt -q --disable-pip-version-check 2>nul
if %errorlevel% neq 0 (
    echo   [!] 依赖安装失败，正在重试...
    python -m pip install -r requirements.txt --disable-pip-version-check
    if %errorlevel% neq 0 (
        echo.
        echo   [!] 安装失败，请检查网络连接后重试
        pause
        exit /b 1
    )
)
echo   [OK] 依赖就绪

REM ── 生成配置文件 ──
if not exist config.yaml (
    copy config.example.yaml config.yaml >nul
    echo   [OK] 已生成 config.yaml
)

REM ── 启动 ──
echo   [OK] 正在启动 PaperPilot...
echo.
python app.py
if %errorlevel% neq 0 (
    echo.
    echo   [!] 程序异常退出
    pause
)
