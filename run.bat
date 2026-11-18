@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Бот обліку накладних

rem ---------- шукаємо Python ----------
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo  [!] Python не знайдено.
    echo      Встановіть Python 3.12 з https://www.python.org/downloads/
    echo      Під час встановлення поставте галочку "Add python.exe to PATH".
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

rem ---------- віртуальне середовище ----------
if not exist ".venv\Scripts\python.exe" (
    echo  Перший запуск: створюю середовище, це займе 1-2 хвилини...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo  [!] Не вдалося створити віртуальне середовище.
        pause
        exit /b 1
    )
)

rem ---------- залежності (ставимо заново, якщо requirements.txt змінився) ----------
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo  Встановлюю бібліотеки...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo  [!] Не вдалося встановити бібліотеки. Перевірте інтернет і спробуйте ще раз.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

rem ---------- файл налаштувань ----------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo  Створено файл налаштувань .env - зараз він відкриється в Блокноті.
    echo  Заповніть BOT_TOKEN і GEMINI_API_KEY, збережіть файл (Ctrl+S)
    echo  і запустіть run.bat ще раз.
    echo.
    start /wait notepad ".env"
    pause
    exit /b 0
)

rem ---------- запуск ----------
echo.
".venv\Scripts\python.exe" main.py
echo.
echo  Бот зупинено.
pause
