$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'

function Assert-Exit([string] $Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step завершился с кодом $LASTEXITCODE" }
}

Push-Location $Root
try {
    if (-not (Test-Path $Python)) {
        python -m venv .venv
        Assert-Exit 'Создание Python venv'
    }
    & $Python -m pip install -r requirements-windows.txt
    Assert-Exit 'Установка Python-зависимостей'
    Push-Location frontend
    try {
        npm ci
        Assert-Exit 'Установка зависимостей UI'
        npm run build
        Assert-Exit 'Сборка интерфейса'
    } finally { Pop-Location }
    & $Python -c "from launcher.tray import tray_icon; tray_icon().save('build-tray.ico', sizes=[(16,16),(32,32),(48,48),(64,64)])"
    Assert-Exit 'Подготовка иконки'
    & $Python -m PyInstaller --noconfirm --onefile --windowed --name 'Prospero Library' --icon build-tray.ico `
        --paths $Root --add-data 'frontend/dist:frontend/dist' --hidden-import pystray._win32 `
        --collect-submodules uvicorn --collect-submodules python_multipart launcher/tray.py
    Assert-Exit 'Сборка EXE'
    Write-Host (Join-Path $Root 'dist\Prospero Library.exe')
} finally {
    Pop-Location
}
