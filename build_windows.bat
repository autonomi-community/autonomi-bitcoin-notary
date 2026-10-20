@echo off
REM Build dist\AutonomiNotary.exe (single-file, windowed, no Python needed on the target).
REM Requires Python 3.10+ from python.org (the "py" launcher).
REM Optional: set ANT_BUNDLE=C:\path\to\ant.exe before running to embed ant.
setlocal
cd /d "%~dp0"

py -m pip install --upgrade -r requirements-build.txt || goto :error
py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller build.spec || goto :error

echo.
echo Built: %CD%\dist\AutonomiNotary.exe
goto :eof

:error
echo Build failed.
exit /b 1
