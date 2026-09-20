#!/command/with-contenv bash
# Foreground entry only. Never nohup, never background, never pgrep.
set -euo pipefail

if [ -x /opt/lard/bin/python ]; then
  PYTHON=/opt/lard/bin/python
else
  PYTHON=python3
fi

echo "lard_controller: exec ${PYTHON} /app/controller.py" >&2
exec "${PYTHON}" -u /app/controller.py
