@echo off
chcp 65001 >nul
cd /d "%~dp0"
where git >nul 2>&1
if errorlevel 1 (
    echo  [!] Git не встановлено. Завантажте його з https://git-scm.com/download/win
    start "" "https://git-scm.com/download/win"
    pause
    exit /b 1
)
echo  Завантажую оновлення з GitHub...
git pull --ff-only
if errorlevel 1 (
    echo  [!] Не вдалося оновити. Надішліть скріншот цього вікна розробнику.
    pause
    exit /b 1
)
echo.
echo  ✅ Бота оновлено. Закрийте вікно бота (якщо відкрите) і запустіть run.bat знову.
echo     Ваші налаштування (.env) і база (invoices.db) не змінювались.
pause
