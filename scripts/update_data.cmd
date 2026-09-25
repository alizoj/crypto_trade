@echo off
rem Регулярное обновление данных для планировщика Windows: архив Binance → norm → качество → догрузка гэпов.
rem Лог дописывается в data\logs\update.log. Пример задачи (раз в сутки, 06:00):
rem   schtasks /Create /SC DAILY /ST 06:00 /TN "HashHedge data update" /TR "\"%~f0\""
setlocal
cd /d "%~dp0.."
if not exist data\logs mkdir data\logs
set PYTHONIOENCODING=utf-8
echo ==== %DATE% %TIME% >> data\logs\update.log
.venv\Scripts\quant.exe data update >> data\logs\update.log 2>&1
set RC=%ERRORLEVEL%
echo ==== exit %RC% >> data\logs\update.log
exit /b %RC%
