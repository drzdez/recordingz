@echo off
rem Desktop entry point: import the inbox, then transcribe + export + summarize the newest recording.
title recordingz - zpracovat posledni nahravku
call "%~dp0recordingz.cmd" process --latest %*
echo.
if errorlevel 1 (echo CHYBA - viz vypis vyse. & pause & exit /b 1)
echo Hotovo.
rem Open the review page when somebody is still SPEAKER_xx; returns at once otherwise.
call "%~dp0recordingz.cmd" review latest --only-unresolved
pause
