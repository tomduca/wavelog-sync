@echo off
rem Corre wavelog_sync.py dentro de su entorno virtual, creandolo si hace falta.
rem Uso: wavelog-sync.bat [opciones de wavelog_sync.py]   (ej: wavelog-sync.bat --dry-run)
setlocal

cd /d "%~dp0"
set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "SELLO=%VENV%\.requirements.sha"
rem Salida en UTF-8 aunque la consola use otra codificacion (nombres con acentos, etc.).
set "PYTHONUTF8=1"

if not exist "%PY%" (
    echo Creando entorno virtual en .venv...
    where py >nul 2>nul && (py -3 -m venv "%VENV%") || (python -m venv "%VENV%")
    if not exist "%PY%" (
        echo No se pudo crear el entorno virtual. Instala Python 3.10+ desde python.org
        echo y marca "Add python.exe to PATH". 1>&2
        exit /b 1
    )
)

rem Reinstala dependencias solo cuando cambia requirements.txt (el hash lo compara Python).
set "HASHPY=import hashlib,pathlib,sys; h=hashlib.sha1(pathlib.Path('requirements.txt').read_bytes()).hexdigest(); s=pathlib.Path(sys.argv[2]); sys.exit(s.write_text(h) and 0 if sys.argv[1]=='guardar' else int(not s.exists() or s.read_text().strip()!=h))"
"%PY%" -c "%HASHPY%" comparar "%SELLO%"
if errorlevel 1 (
    echo Instalando dependencias...
    "%PY%" -m pip install --quiet --upgrade pip || exit /b 1
    "%PY%" -m pip install --quiet -r requirements.txt || exit /b 1
    "%PY%" -m playwright install chromium || exit /b 1
    "%PY%" -c "%HASHPY%" guardar "%SELLO%"
)

if not exist ".env" (
    echo Falta .env: copia .env.example a .env y completalo. 1>&2
    exit /b 1
)

"%PY%" wavelog_sync.py %*
exit /b %ERRORLEVEL%
