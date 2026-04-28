"""
Send a backup directory's contents to a Telegram chat via a bot.

Triggered by `make backup` after the local pg_dump + media tarball land
under `backups/<timestamp>/`. Skips silently when BACKUP_BOT_TOKEN /
BACKUP_CHAT_ID are not configured — cron must not fail because the user
hasn't opted into Telegram delivery.

Telethon (already a dependency for the userbot core) handles uploads up
to ~2 GB without the local Bot API server, which Bot API HTTP can't.
"""
import asyncio
import os
import socket
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Upload every file in <directory> to the configured Telegram backup chat."

    def add_arguments(self, parser):
        parser.add_argument(
            'directory', type=str,
            help="Path to the backup directory (e.g., backups/20260428_153000)",
        )

    def handle(self, *args, **opts):
        token = os.environ.get('BACKUP_BOT_TOKEN', '').strip()
        chat = os.environ.get('BACKUP_CHAT_ID', '').strip()
        if not token or not chat:
            self.stdout.write(self.style.WARNING(
                "BACKUP_BOT_TOKEN / BACKUP_CHAT_ID not set — skipping Telegram upload"
            ))
            return

        directory = Path(opts['directory']).resolve()
        if not directory.is_dir():
            raise CommandError(f"Not a directory: {directory}")

        files = sorted(p for p in directory.iterdir() if p.is_file())
        if not files:
            self.stdout.write(self.style.WARNING(f"No files found under {directory}"))
            return

        try:
            chat_id = int(chat)
        except ValueError:
            chat_id = chat  # @username also accepted by Telethon

        try:
            asyncio.run(_upload(token, chat_id, directory.name, files, self.stdout))
        except Exception as e:
            raise CommandError(f"Telegram upload failed: {e}") from e

        self.stdout.write(self.style.SUCCESS(
            f"Uploaded {len(files)} file(s) from {directory.name}"
        ))


async def _upload(token, chat_id, label, files, stdout):
    from telethon import TelegramClient
    from django.conf import settings

    api_id = int(settings.TELEGRAM_API_ID or 0)
    api_hash = settings.TELEGRAM_API_HASH or ''
    if not api_id or not api_hash:
        raise RuntimeError("API_ID / API_HASH must be set in .env")

    hostname = socket.gethostname()
    ts = timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')
    header = f"💾 Userbot Manager backup\n📂 {label}\n🖥️ {hostname}\n🕐 {ts}"

    client = TelegramClient(
        ':memory:', api_id, api_hash,
        device_model='backup-uploader', system_version='1.0',
    )
    await client.start(bot_token=token)
    try:
        await client.send_message(chat_id, header)
        for path in files:
            size_mb = path.stat().st_size / (1024 * 1024)
            caption = f"`{path.name}` — {size_mb:.1f} MB"
            stdout.write(f"  → uploading {path.name} ({size_mb:.1f} MB)…")
            await client.send_file(
                chat_id, str(path),
                caption=caption, parse_mode='md',
                force_document=True,
            )
    finally:
        await client.disconnect()
