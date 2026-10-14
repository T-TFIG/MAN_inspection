@echo off
REM Build MAN_inspection.exe locally (same command CI uses).
python -m pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name MAN_inspection main.py
echo.
echo Built: dist\MAN_inspection.exe
