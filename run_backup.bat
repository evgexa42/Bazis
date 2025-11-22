@echo off
setlocal enableextensions

cd /d "%~dp0"

REM Activate virtual environment (adjust path if your venv differs)
if exist "%~dp0.venv\Scripts\activate.bat" (
    call "%~dp0.venv\Scripts\activate.bat"
) else if exist "%~dp0venv\Scripts\activate.bat" (
    call "%~dp0venv\Scripts\activate.bat"
) else (
    echo Virtual environment not found. Update run_backup.bat with your venv path.
    exit /b 1
)

python backup_db.py
endlocal