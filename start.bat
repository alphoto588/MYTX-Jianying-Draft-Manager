@echo off
rem ============================================================
rem  剪映草稿历史版本管理器 · Windows 一键启动脚本
rem ------------------------------------------------------------
rem  公众号：爱折腾的摄影大叔（lovephoto588）
rem  Copyright (c) 2026 爱折腾的摄影大叔（lovephoto588）
rem  本项目基于 MIT 协议开源，详见仓库根目录的 LICENSE 文件。
rem ============================================================
chcp 936 >nul
cd /d "%~dp0"
set "LOG=%TEMP%\jy_start.log"
set "RC=0"

> "%LOG%" echo === Jianying Draft History Manager start log ===
>>"%LOG%" echo Time: %date% %time%
>>"%LOG%" echo Dir : %~dp0
>>"%LOG%" echo.

if exist "app.py" goto :has_app
>>"%LOG%" echo [ERROR] app.py missing
echo.
echo   [ERROR] 当前目录缺少 app.py
echo.
echo   请下载完整压缩包 JianyingDraftManager.zip，
echo   解压到一个独立文件夹后再双击本文件。
echo   日志: %LOG%
pause
exit /b 1

:has_app
>>"%LOG%" echo [1/3] app.py found
set "PY="
where python >nul 2>nul
if errorlevel 1 goto :try_py
python -V >nul 2>&1
if errorlevel 1 goto :try_py
set "PY=python"
goto :run

:try_py
>>"%LOG%" echo [2/3] python unusable, trying py
where py >nul 2>nul
if errorlevel 1 goto :try_scan
py -3 -V >nul 2>&1
if errorlevel 1 goto :try_scan
set "PY=py -3"
goto :run

:try_scan
>>"%LOG%" echo [2/3] py unusable, scanning install dirs
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :probe "%%~fd\python.exe"
for /d %%d in ("C:\Python3*") do call :probe "%%~fd\python.exe"
for /d %%d in ("%ProgramFiles%\Python3*") do call :probe "%%~fd\python.exe"
if not defined PY goto :no_python

:run
>>"%LOG%" echo [3/3] python = %PY%
echo.
echo   [OK] Python: %PY%
%PY% -V
echo.
echo   正在启动服务...
echo   浏览器会自动打开；未打开请手动访问 http://127.0.0.1:8620
echo   停止服务请按 Ctrl+C
echo   ------------------------------------------
>>"%LOG%" echo launching app.py
start "" /min cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:8620"
%PY% app.py --port 8620 >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo.
echo   服务已停止 (退出码 %RC%)
echo   日志: %LOG%
pause
exit /b %RC%

:probe
if defined PY goto :eof
if not exist "%~1" goto :eof
"%~1" -V >nul 2>&1
if errorlevel 1 goto :eof
set "PY="%~1""
goto :eof

:no_python
>>"%LOG%" echo [ERROR] no python found
echo.
echo   [错误] 没有找到可用的 Python
echo.
echo   1. 打开 https://www.python.org/downloads/windows/ 安装 Python 3.8+
echo   2. 安装时务必勾选 "Add python.exe to PATH"
echo   3. 装完重新双击本文件
echo.
echo   已装过的话，请在 PowerShell 执行并把截图发我:
echo     where.exe python
echo     where.exe py
echo     py -3 -V
echo.
echo   日志: %LOG%
pause
exit /b 1
