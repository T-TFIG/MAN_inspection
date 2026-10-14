@echo off
REM Build both exes locally (same commands CI uses).
python -m pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name MAN_inspection main.py
pyinstaller --onefile --noconsole --name MAN_inspection_admin admin.py
echo.
echo Built: dist\MAN_inspection.exe  (employee logger)
echo Built: dist\MAN_inspection_admin.exe  (admin console)
