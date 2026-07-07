@echo off
echo Installing OpenReel Studio...

where node >nul 2>nul
if %errorlevel% neq 0 (
  echo Node.js 20+ is required. Please install from https://nodejs.org
  exit /b 1
)

where pnpm >nul 2>nul
if %errorlevel% neq 0 (
  echo Installing pnpm...
  npm install -g pnpm
)

where python >nul 2>nul
if %errorlevel% neq 0 (
  echo Python 3.11+ is required. Please install from https://python.org
  exit /b 1
)

if not exist data mkdir data
if not exist assets mkdir assets
if not exist config mkdir config
if not exist plugins mkdir plugins
if not exist skills mkdir skills
if not exist skills\workflows mkdir skills\workflows
if not exist skills\prompts mkdir skills\prompts
if not exist skills\review mkdir skills\review
if not exist workflow_templates mkdir workflow_templates
if not exist storage mkdir storage
if not exist storage\assets mkdir storage\assets
if not exist storage\exports mkdir storage\exports
if not exist storage\temp mkdir storage\temp

echo Installing frontend dependencies...
call pnpm install

echo Installing backend dependencies...
cd apps\api
where uv >nul 2>nul
if %errorlevel% neq 0 (
  pip install uv
)
uv sync
uv run python ..\..\scripts\init_db.py
cd ..\..

echo.
echo Installation completed!
echo API keys are managed via config/runtime.jsonc
echo Start the API: pnpm api:dev
echo Start the Web app in another terminal: pnpm dev
