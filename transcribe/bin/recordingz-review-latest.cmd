@echo off
rem Desktop entry point: open the local review page (name the speakers by ear) for the newest recording.
title recordingz - zkontrolovat mluvci
call "%~dp0recordingz.cmd" review latest %*
if errorlevel 1 (echo CHYBA - viz vypis vyse. & pause)
