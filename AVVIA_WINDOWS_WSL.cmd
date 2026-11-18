@echo off
setlocal
title SVN - Git
where wsl.exe >nul 2>&1
if errorlevel 1 (
    echo WSL non e' disponibile. Consulta LEGGIMI.md per l'installazione.
    pause
    exit /b 1
)
set "SVNGIT_SCRIPT=%~dp0svn_git_migrator.py"
set "WSLENV=SVNGIT_SCRIPT/p:%WSLENV%"
wsl.exe --exec python3 -c "import os,runpy;runpy.run_path(os.environ['SVNGIT_SCRIPT'],run_name='__main__')"
if errorlevel 1 (
    echo.
    echo L'app non e' partita oppure ha restituito un errore.
    echo Servono WSL2 con WSLg e, in Ubuntu, questi pacchetti:
    echo sudo apt install python3 python3-tk git subversion
    echo Per altri dettagli leggi LEGGIMI.md.
    pause
)
endlocal
