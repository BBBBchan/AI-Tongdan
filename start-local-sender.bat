@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv-local-sender\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto python_fallback
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto python_error
py -3 -m venv .venv-local-sender
if errorlevel 1 goto failed
goto install
:python_fallback
python -c "import sys; raise SystemExit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto python_error
python -m venv .venv-local-sender
if errorlevel 1 goto failed
:install
".venv-local-sender\Scripts\python.exe" -m pip install -r requirements-local-sender.txt
if errorlevel 1 goto failed
if exist bilibili_cookie.txt goto run
if not "%~1"=="" goto run
echo.
echo 请先在本机浏览器登录 B 站，再在此文件夹的命令提示符运行：
echo .venv-local-sender\Scripts\python.exe -m pip install -U browser-cookie3
echo .venv-local-sender\Scripts\python.exe get_bilibili_cookie.py --browser chrome
echo Chrome 可换成 edge / firefox / safari / brave / chromium。
echo 导出成功后，再双击这个启动脚本。说明见 docs\local-sender.md。
goto failed
:run
".venv-local-sender\Scripts\python.exe" local_sender.py %*
set "sender_result=%errorlevel%"
echo.
pause
exit /b %sender_result%
:python_error
echo 请先安装 Python 3.9 或更新版本，并勾选 Add Python to PATH。
:failed
echo.
pause
exit /b 1
