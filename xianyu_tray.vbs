' 闲鱼助手 - 托盘启动器（wscript 无窗口启动托盘脚本）
' 用法: wscript xianyu_tray.vbs
Set sh = CreateObject("WScript.Shell")
Dim ps, arg
ps = "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "
arg = """" & Replace(WScript.ScriptFullName, "xianyu_tray.vbs", "xianyu_tray.ps1") & """"
sh.Run ps & arg, 0, False
