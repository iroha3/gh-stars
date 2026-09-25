@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  刷新 YOUR_NAME 的 GitHub Star 快照
echo  (重新拉取 -> 覆盖 csv/md/html -> 打开浏览器)
echo ============================================
echo.
python stars.py --user YOUR_NAME -o YOUR_NAME-stars.csv --all
if errorlevel 1 (
  echo.
  echo [x] 出错了，检查一下网络或 gh auth status
  pause
  exit /b 1
)
echo.
echo [ok] 完成，正在打开 HTML ...
start "" "YOUR_NAME-stars.html"
