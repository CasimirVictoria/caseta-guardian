#!/bin/bash
set -e
echo "🚀 Instal·lant Caseta Guardian com a servei d'usuari..."

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/bin"
cp systemd/caseta-guardian.service "$HOME/.config/systemd/user/"

cat << 'BINEOF' > "$HOME/.local/bin/caseta"
#!/bin/sh
exec /usr/bin/uv run --with paho-mqtt python3 "$HOME/Documents/Segon_Cervell/projects/caseta-guardian/src/status.py" "$@"
BINEOF
chmod +x "$HOME/.local/bin/caseta"

systemctl --user daemon-reload
systemctl --user enable caseta-guardian.service
systemctl --user restart caseta-guardian.service

echo "✅ Instal·lació completada amb èxit! El servei està actiu i la comanda 'caseta' disponible."
