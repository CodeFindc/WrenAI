@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Wren FastAPI Server

echo ============================================
echo   Wren FastAPI Multi-Session Server
echo ============================================
echo.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

:: ── Project root detection ──────────────────────────────────────────────────
:: examples dir  = sdk/wren-langchain/examples
:: langchain dir = sdk/wren-langchain
:: core dir      = core/wren
pushd "%SCRIPT_DIR%..\.." >nul
set "LANGCHAIN_DIR=%cd%"
popd >nul
pushd "%SCRIPT_DIR%..\..\..\core\wren" >nul
set "CORE_DIR=%cd%"
popd >nul
echo      Wren Core  : %CORE_DIR%
echo      LangChain  : %LANGCHAIN_DIR%
echo.

:: ── 1. Install / check dependencies (like Dockerfile) ───────────────────────
echo [1/10] Installing / checking dependencies ...

echo   1a. Installing core/wren package ...
pip install -e "%CORE_DIR%" --quiet 2>&1 | findstr /V "^$" | findstr /V "already satisfied" || echo      core/wren already installed.

echo   1b. Installing wren-langchain package (with wren-engine^>wrenai substitution) ...
:: Backup pyproject.toml, replace wren-engine with wrenai (like Dockerfile's sed), then restore
copy "%LANGCHAIN_DIR%\pyproject.toml" "%LANGCHAIN_DIR%\pyproject.toml.bak" >nul
powershell -Command "&{ (Get-Content '%LANGCHAIN_DIR%\pyproject.toml') -replace 'wren-engine', 'wrenai' | Set-Content '%LANGCHAIN_DIR%\pyproject.toml' -Encoding utf8 }" >nul
pip install -e "%LANGCHAIN_DIR%" --quiet 2>&1 | findstr /V "^$" | findstr /V "already satisfied" || echo      wren-langchain already installed.
copy "%LANGCHAIN_DIR%\pyproject.toml.bak" "%LANGCHAIN_DIR%\pyproject.toml" >nul
del "%LANGCHAIN_DIR%\pyproject.toml.bak" >nul 2>&1

echo   1c. Installing requirements.txt ...
pip install -r "%SCRIPT_DIR%requirements.txt" --quiet 2>&1 | findstr /V "^$" | findstr /V "already satisfied" || echo      requirements already satisfied.

echo.

:: ── 2. Load .env ────────────────────────────────────────────────────────────
if exist ".env" (
    echo [2/10] Loading environment variables from .env ...
    for /f "usebackq delims=" %%a in (".env") do (
        set "line=%%a"
        if not "!line:~0,1!"=="#" if not "!line!"=="" (
            for /f "tokens=1,* delims==" %%b in ("!line!") do (
                set "key=%%b"
                set "val=%%c"
                if "!val:~0,1!"==""^"" if "!val:~-1!"==""^"" set "val=!val:~1,-1!"
                set "!key!=!val!"
            )
        )
    )
    echo      Environment variables loaded.
) else (
    echo [2/10] WARNING: .env not found. Copy .env.example to .env and edit it.
    echo      Copying .env.example to .env for reference ...
    copy ".env.example" ".env" >nul 2>&1
)
echo.

:: ── 3. Project directory ────────────────────────────────────────────────────
echo [3/10] Project directory setup ...

set "PROJECT_DIR=%~dp0wren_project"
if not exist "%PROJECT_DIR%" (
    echo      Creating project directory at !PROJECT_DIR! ...
    mkdir "%PROJECT_DIR%"
) else (
    echo      Project directory already exists.
)
set "WREN_HOME=%PROJECT_DIR%\.wren"
if not exist "%WREN_HOME%" mkdir "%WREN_HOME%"
set "WREN_HOME=!WREN_HOME!"
echo      WREN_HOME = !WREN_HOME!
echo.

:: ── 4. Auto-generate profiles.yml from environment variables ─────────────────
echo [4/10] Connection profile ...

set "PROFILES_FILE=%PROJECT_DIR%\.wren\profiles.yml"

:: Only generate if profiles.yml doesn't exist
if not exist "%PROFILES_FILE%" (
    set "datasource=%DATASOURCE%"
    if defined datasource (
        echo      Generating profile '!ACTIVE_PROFILE!' (datasource: !datasource!) ...
        (
            echo active: !ACTIVE_PROFILE!
            echo profiles:
            echo   !ACTIVE_PROFILE!:
            echo     datasource: !datasource!
            if defined DB_HOST echo     host: !DB_HOST!
            if defined DB_PORT echo     port: !DB_PORT!
            if defined DB_NAME echo     database: !DB_NAME!
            if defined DB_USER echo     user: !DB_USER!
            if defined DB_PASSWORD echo     password: !DB_PASSWORD!
            if defined SSL_MODE echo     ssl_mode: !SSL_MODE!
        ) > "%PROFILES_FILE%"

        :: Handle EXTRA_PROFILE_KEYS (JSON) via Python
        if defined EXTRA_PROFILE_KEYS (
            python -c "
import sys, json
try:
    extra = json.loads(sys.argv[1])
    for k, v in extra.items():
        print(f'    {k}: {v}')
except:
    pass  # ignore invalid JSON
" "!EXTRA_PROFILE_KEYS!" >> "%PROFILES_FILE%" 2>nul
        )
        echo      Profile written to !PROFILES_FILE!
    ) else (
        echo      DATASOURCE not set -- skipping profile generation.
    )
) else (
    echo      profiles.yml already exists, skipping.
)
echo.

:: ── 5. Initialize Wren project ──────────────────────────────────────────────
echo [5/10] Project initialization ...

set "PROJECT_FILE=%PROJECT_DIR%\wren_project.yml"
if not exist "%PROJECT_FILE%" (
    echo      Initializing empty project at !PROJECT_DIR! ...
    wren context init --empty --path "%PROJECT_DIR%"
    if !ERRORLEVEL! equ 0 (
        echo      Project initialized successfully.
    ) else (
        echo      WARNING: 'wren context init' failed. Make sure 'wren' CLI is installed.
        echo      Skipping project initialization.
    )
) else (
    echo      Project already exists.
)
echo.

:: ── 6. Build MDL (always on restart) ─────────────────────────────────────────
echo [6/10] MDL build ...

set "MODEL_DIR=%PROJECT_DIR%\models"
if exist "%MODEL_DIR%" (
    dir "%MODEL_DIR%\*" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo      Rebuilding MDL from models/ ...
        wren context build --path "%PROJECT_DIR%"
        if !ERRORLEVEL! equ 0 (
            echo      MDL build complete.
        ) else (
            echo      WARNING: MDL build failed.
        )
    ) else (
        echo      Models directory is empty -- skipping MDL build.
    )
) else (
    echo      No models directory -- skipping MDL build.
)
echo.

:: ── 7. Memory index (always on restart) ──────────────────────────────────────
echo [7/10] Memory index ...

set "MDL_JSON=%PROJECT_DIR%\target\mdl.json"
if exist "%MDL_JSON%" (
    echo      Indexing MDL schema into LanceDB memory ...
    wren memory index --path "%PROJECT_DIR%\.wren\memory" --mdl "%MDL_JSON%" --no-seed
    if !ERRORLEVEL! equ 0 (
        echo      Memory indexing complete.
    ) else (
        echo      WARNING: Memory indexing failed (memory extra may not be installed).
        echo      To install: pip install "wrenai[memory]"
    )
) else (
    echo      No MDL build found -- skipping memory index.
)
echo.

:: ── 8. Configuration summary ────────────────────────────────────────────────
echo [8/10] Configuration summary ...
echo.
echo      PROJECT_PATH = %PROJECT_DIR%
echo      WREN_HOME    = !WREN_HOME!
echo      Profiles     = !PROFILES_FILE!
echo.
echo      To add a model:  wren context import ...
echo      To validate:     wren context validate --path %PROJECT_DIR%
echo      To build:        wren context build --path %PROJECT_DIR%
echo      To query:        wren --sql 'SELECT ...'
echo.

:: ── 9. Load .env variables (second pass to pick up pip-installed extras) ─────
echo [9/10] Reloading .env for server runtime ...

endlocal & setlocal enabledelayedexpansion
cd /d "%SCRIPT_DIR%"
set "PROJECT_DIR=%~dp0wren_project"
for %%I in ("%SCRIPT_DIR%..\..") do set "LANGCHAIN_DIR=%%~fI"

if exist ".env" (
    for /f "usebackq delims=" %%a in (".env") do (
        set "line=%%a"
        if not "!line:~0,1!"=="#" if not "!line!"=="" (
            for /f "tokens=1,* delims==" %%b in ("!line!") do (
                set "key=%%b"
                set "val=%%c"
                if "!val:~0,1!"==""^"" if "!val:~-1!"==""^"" set "val=!val:~1,-1!"
                set "!key!=!val!"
            )
        )
    )
)
set "PROJECT_PATH=%PROJECT_DIR%"
echo.

:: ── 10. Start FastAPI server ────────────────────────────────────────────────
echo [10/10] Starting FastAPI server ...
echo.
echo      ^> http://0.0.0.0:8201
echo      ^> PROJECT_PATH=%PROJECT_DIR%
echo.

set "PROJECT_PATH=%PROJECT_DIR%"

python langgraph_fastapi_multi.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Server exited with code %ERRORLEVEL%
    pause
)

endlocal
