@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  Refresh GitHub Star snapshot
echo ============================================
echo.
echo  导出对象：命令参数 ^> .stars-user 文件 ^> 当前 gh 登录账号
echo.

set USERARG=
if not "%~1"=="" set USERARG=--user %~1

python stars.py %USERARG% -o stars.csv --all
if errorlevel 1 (
  echo.
  echo [x] 出错了，检查一下网络，或运行 gh auth status
  pause
  exit /b 1
)
echo.
echo [ok] 完成，正在打开 HTML ...
start "" "stars.html"
