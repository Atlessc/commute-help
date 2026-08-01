#!/bin/zsh

set -u
cd -- "${0:A:h}"

echo "Commute Help"
echo "============"

if [[ ! -x .venv/bin/python || ! -x node_modules/.bin/concurrently || ! -d frontend/node_modules ]]; then
  echo "First-time setup is required. Running npm run setup..."
  npm run setup || {
    echo ""
    echo "Setup failed. Review the message above, then press Return to close."
    read -r
    exit 1
  }
fi

npm run dev
status=$?

if [[ $status -ne 0 ]]; then
  echo ""
  echo "Commute Help stopped with an error. Run npm run doctor for recovery guidance."
  echo "Press Return to close this window."
  read -r
fi

exit $status
