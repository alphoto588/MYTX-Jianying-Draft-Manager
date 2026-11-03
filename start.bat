@echo off
rem ============================================================
rem  剪映草稿历史版本管理器 · Windows 一键启动脚本
rem ------------------------------------------------------------
rem  公众号：爱折腾的摄影大叔（lovephoto588）
rem  Copyright (c) 2026 爱折腾的摄影大叔（lovephoto588）
rem  本项目基于 MIT 协议开源，详见仓库根目录的 LICENSE 文件。
rem ============================================================
chcp 65001 >nul
title Jianying Draft History Manager
cd /d "%~dp0"
if not exist "%PY%" set "PY=python"
echo Starting Jianying Draft History Manager...
"%PY%" app.py --port 8620
pause
