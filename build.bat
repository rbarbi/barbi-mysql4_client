@echo off
REM Gera dist\BarbiMysql4Client.exe (executavel unico, sem precisar de Python instalado)
setlocal
pushd "%~dp0"
set VENV=%LOCALAPPDATA%\mysql4client-build\venv
if not exist "%VENV%\Scripts\python.exe" (
    python -m venv "%VENV%" || goto :erro
)
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pyinstaller || goto :erro
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed ^
    --name BarbiMysql4Client ^
    --workpath "%LOCALAPPDATA%\mysql4client-build\work" ^
    --specpath "%LOCALAPPDATA%\mysql4client-build" ^
    --distpath "%~dp0dist" ^
    mysql4client.py || goto :erro
echo.
echo Pronto: %~dp0dist\BarbiMysql4Client.exe
popd
exit /b 0
:erro
echo Falha no build.
popd
exit /b 1
