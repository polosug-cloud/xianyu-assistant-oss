@echo off
rem 闲鱼助手 启动（任务栏托盘·无窗口）
cd /d "%~dp0"
start "" wscript.exe "%~dp0xianyu_tray.vbs"
exit
