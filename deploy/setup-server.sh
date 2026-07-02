#!/usr/bin/env bash
# Первичная настройка сервера (Ubuntu 22.04/24.04) под CI/CD-деплой.
# Запускать под root один раз: bash setup-server.sh
set -euo pipefail

DEPLOY_DIR=/opt/expaw
DEPLOY_USER=deploy

echo "==> Docker + rsync"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
apt-get install -y -q rsync

echo "==> Пользователь для деплоя"
if ! id "$DEPLOY_USER" &>/dev/null; then
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

echo "==> Каталог проекта"
mkdir -p "$DEPLOY_DIR/data/session"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR"

echo "==> SSH-ключ для GitHub Actions"
sudo -u "$DEPLOY_USER" mkdir -p "/home/$DEPLOY_USER/.ssh"
KEYFILE="/home/$DEPLOY_USER/.ssh/github_actions"
if [ ! -f "$KEYFILE" ]; then
    sudo -u "$DEPLOY_USER" ssh-keygen -t ed25519 -N "" -f "$KEYFILE" -C "github-actions-deploy"
    cat "$KEYFILE.pub" >> "/home/$DEPLOY_USER/.ssh/authorized_keys"
    chown "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh/authorized_keys"
    chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"
fi

echo
echo "Готово. Дальше:"
echo "1. Скопируй на свою машину ПРИВАТНЫЙ ключ и добавь его в GitHub:"
echo "   Settings -> Secrets and variables -> Actions -> New repository secret"
echo "     SSH_KEY  = содержимое $KEYFILE (весь файл целиком)"
echo "     SSH_HOST = IP этого сервера"
echo "     SSH_USER = $DEPLOY_USER"
echo "2. Положи конфиг и сессию:"
echo "     $DEPLOY_DIR/.env                        (из .env.example)"
echo "     $DEPLOY_DIR/data/session/userbot.session (сгенерирован локально, chmod 600)"
echo "3. Запушь в main — GitHub Actions задеплоит сам."
