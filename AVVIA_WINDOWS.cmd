@echo off
setlocal DisableDelayedExpansion
title SVN - Git - Windows
set "SVNGIT_SCRIPT=%~dp0svn_git_migrator.py"
set "SVNGIT_PYTHON="
set "SVNGIT_PYARG="
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="

if not exist "%SVNGIT_SCRIPT%" (
    echo File svn_git_migrator.py non trovato accanto a questo launcher.
    echo Estrai tutto il contenuto dello ZIP nella stessa cartella.
    pause
    exit /b 1
)

for /f "delims=" %%P in ('where.exe py.exe 2^>nul') do (
    set "SVNGIT_CANDIDATE=%%P"
    call :try_launcher
    if defined SVNGIT_PYTHON goto run
)

for /f "delims=" %%P in ('where.exe python.exe python3.exe 2^>nul') do (
    set "SVNGIT_CANDIDATE=%%P"
    call :try_python
    if defined SVNGIT_PYTHON goto run
)

for /d %%D in ("%LocalAppData%\Programs\Python\Python*" "%ProgramFiles%\Python*" "%ProgramFiles(x86)%\Python*" "%SystemDrive%\Python*") do (
    set "SVNGIT_CANDIDATE=%%~fD\python.exe"
    call :try_python
    if defined SVNGIT_PYTHON goto run
)

echo.
echo Python nativo Windows 3.10 o successivo, con Tkinter, non trovato.
echo Se Python e' gia' installato, prova da PowerShell uno di questi comandi:
echo   py -3 -m tkinter
echo   python3.exe -m tkinter
echo   python.exe -m tkinter
echo L'opzione -3 si usa soltanto con py, non con python.exe o python3.exe.
echo Altrimenti installa Python per Windows da https://www.python.org/downloads/windows/
echo L'installazione deve includere Tcl/Tk; con l'installer classico scegli Tcl/Tk and IDLE.
echo Questo launcher usa Windows: il Python installato in Ubuntu/WSL non viene usato.
echo Non e' stato installato o modificato alcun programma.
pause
exit /b 1

:try_launcher
if not exist "%SVNGIT_CANDIDATE%" exit /b 0
"%SVNGIT_CANDIDATE%" -3 -c "import sys, tkinter; print('SVNGIT_PYTHON_OK') if sys.platform == 'win32' and sys.version_info >= (3, 10) else sys.exit(1)" 2>nul | "%SystemRoot%\System32\findstr.exe" /x /c:"SVNGIT_PYTHON_OK" >nul
if errorlevel 1 exit /b 0
set "SVNGIT_PYTHON=%SVNGIT_CANDIDATE%"
set "SVNGIT_PYARG=-3"
exit /b 0

:try_python
if not exist "%SVNGIT_CANDIDATE%" exit /b 0
rem Anche WindowsApps puo' risolvere a un Python realmente installato.
rem Controlliamo versione, piattaforma e Tkinter eseguendo il probe.
"%SVNGIT_CANDIDATE%" -c "import sys, tkinter; print('SVNGIT_PYTHON_OK') if sys.platform == 'win32' and sys.version_info >= (3, 10) else sys.exit(1)" 2>nul | "%SystemRoot%\System32\findstr.exe" /x /c:"SVNGIT_PYTHON_OK" >nul
if errorlevel 1 exit /b 0
set "SVNGIT_PYTHON=%SVNGIT_CANDIDATE%"
set "SVNGIT_PYARG="
exit /b 0

:run
echo Avvio con: "%SVNGIT_PYTHON%" %SVNGIT_PYARG%
"%SVNGIT_PYTHON%" %SVNGIT_PYARG% -X utf8 "%SVNGIT_SCRIPT%" %*
set "SVNGIT_EXIT=%ERRORLEVEL%"
if not "%SVNGIT_EXIT%"=="0" (
    echo.
    echo L'app ha restituito un errore. Il dettaglio e' riportato sopra.
    echo Per controllare i requisiti, da PowerShell esegui:
    echo .\AVVIA_WINDOWS.cmd --self-check
    echo Leggi anche LEGGIMI.md nella stessa cartella.
    pause
)
exit /b %SVNGIT_EXIT%
