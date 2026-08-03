@echo off
cd /d "%~dp0"
python txt_to_epub_gui_2.py
if errorlevel 1 pause
