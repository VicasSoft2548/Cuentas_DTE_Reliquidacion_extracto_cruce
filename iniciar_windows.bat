@echo off
setlocal EnableExtensions EnableDelayedExpansion

title Sistema de Cuentas por Cobrar

REM =========================================================
REM ENTRAR A LA CARPETA DONDE ESTA ESTE BAT
REM Funciona tambien desde carpeta compartida UNC
REM =========================================================

pushd "%~dp0"

echo.
echo =============================================
echo        SISTEMA DE CUENTAS POR COBRAR
echo =============================================
echo.


REM =========================================================
REM BUSCAR PYTHON 3.11
REM =========================================================

set "PYTHON="

for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do (
    set "PYTHON=%%P"
)

if not defined PYTHON (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    )
)

if not defined PYTHON (
    echo.
    echo ERROR: Python 3.11 no esta instalado.
    echo.
    pause
    popd
    exit /b 1
)

echo Python encontrado:
"%PYTHON%" --version


REM =========================================================
REM COMPROBAR SI YA ESTA CORRIENDO
REM =========================================================

curl.exe -s --connect-timeout 1 ^
"http://127.0.0.1:5000/api/summary" >nul 2>&1

if not errorlevel 1 (
    echo.
    echo El sistema ya esta iniciado.
    start "" "http://127.0.0.1:5000"
    popd
    exit /b 0
)


REM =========================================================
REM INICIAR APP
REM =========================================================

echo.
echo Iniciando aplicacion...

start "Cuentas por Cobrar - NO CERRAR" ^
"%PYTHON%" "%CD%\app.py"


REM =========================================================
REM ESPERAR A QUE EL SERVIDOR RESPONDA
REM =========================================================

set /a INTENTOS=0

:ESPERAR

timeout /t 1 /nobreak >nul

curl.exe -s --connect-timeout 1 ^
"http://127.0.0.1:5000/api/summary" >nul 2>&1

if not errorlevel 1 goto LISTO

set /a INTENTOS+=1

if !INTENTOS! GEQ 15 (
    echo.
    echo =============================================
    echo ERROR
    echo =============================================
    echo.
    echo La aplicacion no inicio correctamente.
    echo.
    echo Mira la ventana:
    echo "Cuentas por Cobrar - NO CERRAR"
    echo.
    echo Alli aparecera el error real de Python.
    echo.
    pause
    popd
    exit /b 1
)

goto ESPERAR


:LISTO

echo.
echo =============================================
echo       SISTEMA INICIADO CORRECTAMENTE
echo =============================================
echo.
echo http://127.0.0.1:5000
echo.

start "" "http://127.0.0.1:5000"

popd
exit /b 0