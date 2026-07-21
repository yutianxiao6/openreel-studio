#!/usr/bin/env bash
set -e

echo "Installing OpenReel Studio..."

if [ -f .gitmodules ]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "Git is required to initialize bundled source dependencies."
    exit 1
  fi
  git submodule update --init --recursive
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required. Please install Node.js 20+."
  exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
  echo "Installing pnpm..."
  npm install -g pnpm
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11+ is required."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  pip install uv
fi

mkdir -p \
  data \
  storage/assets \
  storage/exports \
  storage/temp \
  assets \
  config \
  plugins \
  skills/workflows \
  skills/prompts \
  skills/review \
  workflow_templates

echo "Installing frontend dependencies..."
pnpm install

echo "Installing backend dependencies..."
cd apps/api
uv sync
cd ../..

echo "Initializing database..."
cd apps/api
uv run python ../../scripts/init_db.py
cd ../..

echo ""
echo "Installation completed."
echo "API keys are managed via config/runtime.jsonc"
echo "Start the API: pnpm api:dev"
echo "Start the Web app in another terminal: pnpm dev"
