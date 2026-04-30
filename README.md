# Userbot Manager

Ko'p akkauntli Telegram (MTProto / Telethon) avtomatlashtirish paneli.
Django 6 + Telethon 1.43 asosida, ko'p-foydalanuvchili (multi-tenant)
arxitektura, xavfsizlik qatlamlari va to'liq Docker deploy.

> **Asosiy holatlar:** 100–200 akkauntni boshqarish, bulk operatsiyalar, jonli
> progress kuzatuvi, kunlik byudjet, proxy, scheduler, statistika dashboard.

---

## Mundarija

1. [Imkoniyatlar](#imkoniyatlar)
2. [Arxitektura](#arxitektura)
3. [Tezkor boshlash](#tezkor-boshlash)
4. [Lokal kompyuterda (Windows)](#lokal-kompyuterda-windows)
5. [Lokal kompyuterda Docker bilan](#lokal-kompyuterda-docker-bilan)
6. [Serverga o'rnatish (production + subdomain)](#serverga-ornatish)
7. [Boshqarish — kundalik amallar](#boshqarish--kundalik-amallar)
8. [.env to'liq sozlamalar](#env-tolqliq-sozlamalar)
9. [Backup va restore](#backup-va-restore)
10. [Xavfsizlik](#xavfsizlik)
11. [Muammolarni tuzatish](#muammolarni-tuzatish)

---

## Imkoniyatlar

### 9 ta tayyor avtomatlashtirish (runner)

| Runner | Nima qiladi |
|--------|-------------|
| `create_groups` | Random nomli supergroup yaratish |
| `create_channels` | Random nomli broadcast kanal yaratish |
| `join_channel` | `@user`, `t.me/+hash`, `joinchat/` qo'llab-quvvatlanadi |
| `boost_views` | Xabarlar `view` sonini oshirish (raundlar bilan) |
| `react_to_post` | Random emoji bilan reaksiya, ehtimollik bilan |
| `vote_poll` | So'rovnomada ovoz berish (random / fixed) |
| `press_start` | Bot deep-link `/start` (referral param) |
| `account_warming` | Vaqt-chegaralangan organik faoliyat (warming) |
| `run_script` | Maxsus Python kod (faqat admin) |

### Infrastruktura

- **Multi-tenant izolatsiya** — har queryset `owner=user` bilan scoped
- **Field-level encryption** — `session_string` va `2FA password` Fernet bilan shifrlangan
- **Per-akkaunt proxy** — SOCKS5 va MTProxy
- **Kunlik byudjet** — har akkaunt kuniga N ops, lazy reset
- **Warm-up gate** — yangi sessiyani himoyalash
- **FloodWait retry** — backoff bilan
- **Sessiya o'lim aniqlash** — `is_active=False` avtomat
- **Scheduler** — `scheduled_at` + `recurring_cron`
- **Live progress** — AJAX polling, per-akkaunt log oqimi
- **Statistika dashboard** — Chart.js bilan time-series
- **Background worker** — alohida `manage.py run_worker` jarayoni

---

## Arxitektura

```
        ┌──────────────────────────────┐
        │   HOST nginx (system daemon) │  443/80 — certbot auto SSL
        │   userbots.omadli.uz → :8000 │  (boshqa subdomenlarni → :8001, :8002, …)
        └──────────────┬───────────────┘
                       │  proxy_pass http://127.0.0.1:8000
            ┌──────────┴──────────┐
            │   docker compose    │   (faqat 127.0.0.1:8000 ga bind)
            │                     │
     ┌──────▼──────┐       ┌──────▼──────┐
     │   web       │       │   worker    │
     │ uvicorn     │       │ run_worker  │
     │ Django ASGI │       │ asyncio loop│
     └──────┬──────┘       └──────┬──────┘
            │                     │
            └──────────┬──────────┘
                       │
                ┌──────▼──────┐
                │ PostgreSQL  │  ← persistent volume (postgres_data)
                └─────────────┘
```

- **host nginx** — VPS'da systemd servis sifatida ishlaydi, 80/443 ni
  egallaydi va har qanday docker loyihaga (`userbots`, kelajakdagi
  subdomenlar) `127.0.0.1:<unique_port>` orqali yo'naltiradi
- **web** — Django ASGI server, faqat `127.0.0.1:${WEB_BIND_PORT}` ga
  bind bo'ladi (server'ning public IP'siga emas) — boshqa loyihalar
  bilan port konflikti yo'q
- **worker** — `pending` task'larni navbatdan oladi, Telethon bilan ishlaydi
- **db** — PostgreSQL (prod) yoki SQLite (lokal dev)

---

## Tezkor boshlash

### 1️⃣ Lokal kompyuterda eng tez (Windows)

```bash
# 1. Virtualenv + bog'liqliklar (allaqachon mavjud loyihada)
venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. .env yarating
cp .env.example .env
# Ichida API_ID/API_HASH ni my.telegram.org/auth dan oling

# 3. Secret'larni yarating
make secrets        # — terminal'ga chiqaradi, .env'ga ko'chiring
# yoki qo'lda:
# venv/Scripts/python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
# venv/Scripts/python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. Migratsiyalar va admin user
venv/Scripts/python.exe manage.py migrate
venv/Scripts/python.exe manage.py createsuperuser

# 5. Ikkita terminalda:
# Terminal 1:
venv/Scripts/python.exe manage.py runserver
# Terminal 2:
venv/Scripts/python.exe manage.py run_worker
```

`http://localhost:8000` — login `superuser` bilan.

### 2️⃣ Production Docker deploy (subdomain bilan)

```bash
# Server'da (Ubuntu/Debian):
git clone <your-repo> userbot-manager
cd userbot-manager

cp .env.docker.example .env
# .env ni tahrirlang — SECRET_KEY, DB_ENCRYPTION_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS


docker compose up -d
docker compose exec web /app/entrypoint.sh manage createsuperuser

# Host nginx + certbot — public domen uchun (bir martalik):
sudo cp nginx/userbots.omadli.uz.conf /etc/nginx/sites-available/
sudo ln -sf /etc/nginx/sites-available/userbots.omadli.uz.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d userbots.omadli.uz --redirect
```

---

## Lokal kompyuterda (Windows)

### Talab qilinadigan

- Python **3.13+** (3.11/3.12 ham ishlaydi)
- Git Bash (yoki PowerShell — Makefile uchun bash maslahat)
- Telegram API ID/Hash — https://my.telegram.org/auth → API development tools

### Birinchi marta o'rnatish

```bash
git clone <your-repo>
cd "Userbot manager"

# Virtualenv yarating
python -m venv venv
venv/Scripts/activate     # Windows
# yoki: source venv/bin/activate (Linux/Mac)

# Bog'liqliklar
pip install -r requirements.txt
```

> **Eslatma**: `tgcrypto` o'rnatilmasa muammo emas — Telethon `pyaes`'ga
> fallback qiladi (sekinroq, lekin to'liq ishlaydi). Tezroq ishlatish uchun
> Linux/Mac'da `pip install tgcrypto` qiling.

### .env

`.env.example` ni nusxalang va ma'lumotlarni to'ldiring:

```env
API_ID=1234567
API_HASH=abc123def456
SECRET_KEY='<random 50 belgi>'
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
DB_ENCRYPTION_KEY=<Fernet kalit>
DB_ENGINE=sqlite
```

Secret'larni avtomat yaratish:

```bash
# SECRET_KEY
venv/Scripts/python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# DB_ENCRYPTION_KEY
venv/Scripts/python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Migratsiyalar va admin

```bash
venv/Scripts/python.exe manage.py migrate
venv/Scripts/python.exe manage.py createsuperuser
```

### Ishga tushirish

**Ikkita terminal kerak:**

```bash
# Terminal 1 — web server
venv/Scripts/python.exe manage.py runserver

# Terminal 2 — background worker
venv/Scripts/python.exe manage.py run_worker
```

Yoki Makefile bilan:

```bash
make native-runserver
# boshqa terminalda:
make native-worker
```

`http://localhost:8000/` — login.

### Tegishli URL'lar

- `/admin/` — Django admin (foydalanuvchi yaratish)
- `/accounts/` — akkauntlar dashboard
- `/jobs/` — vazifalar ro'yxati
- `/jobs/stats/` — statistika
- `/jobs/pools/` — nom pool'lari
- `/accounts/proxies/` — proxy CRUD

---

## Lokal kompyuterda Docker bilan

Agar siz Docker'da test qilmoqchi bo'lsangiz (production'ga yaqinroq, lekin
nginx/SSL'siz):

```bash
cp .env.example .env       # API_ID/API_HASH/SECRET_KEY/DB_ENCRYPTION_KEY to'ldiring

docker compose -f docker-compose.dev.yml up --build

# Boshqa terminalda — admin user
docker compose -f docker-compose.dev.yml exec web /app/entrypoint.sh manage createsuperuser
```

`http://localhost:8000` — Django o'zi servis qiladi (proxy'siz).

To'xtatish:

```bash
docker compose -f docker-compose.dev.yml down
```

---

## Serverga o'rnatish

> Production deploy `userbots.omadli.uz` subdomain bilan, host nginx + certbot
> SSL bilan. Bir VPS'da bir nechta docker loyihalarini parallel servis
> qilish uchun moslangan (har biri o'z 127.0.0.1 portida).

### Talablar

- VPS (Ubuntu 22.04/24.04, Debian 12, AlmaLinux 9 — har qaysi Docker qo'llaydigan distribution)
- 1 GB RAM minimum (2 GB tavsiya), 10 GB disk
- Docker 24+ va Docker Compose v2
- nginx + certbot (host darajasida — docker'da emas)
- 80 va 443 portlar **ochiq** (Let's Encrypt HTTP-01 challenge uchun)
- Domen: `userbots.omadli.uz` → server'ning public IP'siga A record

### 1. Docker o'rnatish (agar yo'q bo'lsa)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Logout / login
```

### 2. Loyihani klonlash

```bash
mkdir -p ~/apps && cd ~/apps
git clone <your-repo> userbot-manager
cd userbot-manager
```

### 3. DNS

`omadli.uz` boshqaruv panelida:
- A record: `userbots` → server'ning IP'si
- TTL: 300 (tez tarqalishi uchun, keyin oshirsangiz bo'ladi)

DNS tarqalishini tekshirish:
```bash
dig +short userbots.omadli.uz
# yoki
nslookup userbots.omadli.uz
```

### 4. .env

```bash
cp .env.docker.example .env
nano .env
```

Mukammal to'ldirish (sample):

```env
# Site
SITE_DOMAIN=userbots.omadli.uz
ALLOWED_HOSTS=userbots.omadli.uz

# Telegram
API_ID=1234567
API_HASH=abc123def456789

# Django
SECRET_KEY='<run: docker run --rm python:3.13-slim python -c "import secrets; print(secrets.token_urlsafe(50))">'
DEBUG=False
SECURE_BEHIND_PROXY=True

# Encryption (BACKUP THIS BEFORE FIRST DEPLOY)
DB_ENCRYPTION_KEY=<run: docker run --rm python:3.13-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'">

# Postgres
DB_ENGINE=postgres
POSTGRES_DB=userbots
POSTGRES_USER=userbots
POSTGRES_PASSWORD=<run: openssl rand -base64 32>
POSTGRES_HOST=db
POSTGRES_PORT=5432

# Tuning
WEB_WORKERS=2
```

> ⚠️ **DB_ENCRYPTION_KEY ni alohida xavfsiz joyda saqlang**
> (parollar menejeri, alohida shifrlangan backup). Yo'qolsa — barcha
> saqlangan Telegram sessiyalari o'qib bo'lmaydigan bo'lib qoladi va siz
> har akkauntni qaytadan login qilishga majbur bo'lasiz.

### 5. Docker stack'ni ishga tushirish

```bash
docker compose up -d
```

Birinchi marta:
- Image build qilinadi (~2-3 daqiqa)
- Postgres o'zini ishga tushiradi
- `init` servisi `migrate` + `collectstatic` ni bajarib chiqadi (`Exit 0`)
- `web` + `worker` ko'tariladi (init muvaffaqiyatli tugagan bo'lsa)
- Web container faqat `127.0.0.1:8000` ga bind bo'ladi (server'ning
  public IP'siga emas) — public reach hali yo'q

```bash
# Web ichkaridan javob beryaptimi tekshirish:
curl -sf http://127.0.0.1:8000/healthz
# → {"status":"ok"}
```

### 6. Host nginx + SSL (bir martalik)

Bu qadam **server'da systemd nginx**'ni sozlaydi. Birinchi marta
o'rnatishda; keyin yangi loyihalar qo'shganda qayta ishlatasiz.

```bash
# Agar nginx + certbot hali yo'q bo'lsa:
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx

# Hech bir docker container 80/443 ni egallamasligi shart:
sudo ss -tlnp | grep -E ':80|:443'
# → faqat `nginx` chiqishi kerak

# Loyihaning nginx config'ini joyiga qo'yish:
sudo cp ~/apps/userbot-manager/nginx/userbots.omadli.uz.conf \
        /etc/nginx/sites-available/
sudo ln -sf /etc/nginx/sites-available/userbots.omadli.uz.conf \
            /etc/nginx/sites-enabled/

# Welcome page'ni o'chirish (bir marta):
sudo rm -f /etc/nginx/sites-enabled/default

# Config'da APP_ROOT yo'lini tekshiring (default: /home/ubuntu/apps/userbot-manager).
# Validate + reload:
sudo nginx -t && sudo systemctl reload nginx

# Let's Encrypt sertifikat (certbot fayl ichini avtomat tahrirlaydi —
# 443 listener va sertifikat yo'llarini qo'shadi):
sudo certbot --nginx -d userbots.omadli.uz --redirect

# Avtomat-renewal certbot.timer orqali sozlangan:
sudo systemctl list-timers | grep certbot
```

Tekshirish:
```bash
curl -s https://userbots.omadli.uz/healthz
# → {"status":"ok"}

make check-deploy
# → System check identified no issues (0 silenced).
```

### 7. Bitta VPS'da bir nechta docker loyihalarini parallel ishlatish

Asosiy g'oya: **har loyiha o'z `127.0.0.1:<unique_port>`'iga bind bo'ladi**,
public 80/443 esa **doim host nginx**'da qoladi.

Yangi loyiha qo'shish:
1. Loyihaning `docker-compose.yml`'ida `web` servisi `127.0.0.1:8001:8000`
   (yoki `8002`, `8003`...) ga bind bo'lsin — har biri unique localhost port.
2. Yangi nginx server bloki yarating (shu repo'dagi
   `nginx/userbots.omadli.uz.conf`'ni shablon sifatida ishlatib):
   ```bash
   sudo cp /etc/nginx/sites-available/userbots.omadli.uz.conf \
           /etc/nginx/sites-available/blog.example.uz.conf
   sudo nano /etc/nginx/sites-available/blog.example.uz.conf
   # `server_name`'ni o'zgartiring + barcha `proxy_pass`'larni 127.0.0.1:8001 ga
   sudo ln -sf /etc/nginx/sites-available/blog.example.uz.conf \
               /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d blog.example.uz --redirect
   ```
3. Tamom — ikkala loyiha 80/443 ni baham ko'rmaydi, host nginx
   `Host:` headeri bo'yicha to'g'ri konteynerga yo'naltiradi.

### 8. Birinchi admin foydalanuvchi

```bash
docker compose exec web /app/entrypoint.sh manage createsuperuser
```

So'raydi:
- Username
- Email (ixtiyoriy)
- Password

Endi `https://userbots.omadli.uz/admin/` ga kirib, qo'shimcha foydalanuvchilar yarating
(har bir foydalanuvchi o'z akkauntlarini ko'radi — multi-tenant).

### 9. Yangi update qilish

```bash
cd ~/apps/userbot-manager
git pull

# Image qayta build + restart
docker compose up -d --build

# Migratsiyalar avtomat — agar qo'lda kerak bo'lsa:
docker compose exec web /app/entrypoint.sh manage migrate
```

---

## Boshqarish — kundalik amallar

### Statuslar

```bash
# Container'lar holati
docker compose ps

# Resurs ishlatishi
docker stats

# Disk hajmi
docker system df
```

### Logni ko'rish

Container stdout (Docker'da, json-file driver, `max-size=10m × max-file=5` bilan rotatsiya):

```bash
make logs                          # web + worker
docker compose logs -f web         # faqat web
docker compose logs -f worker      # faqat worker
docker compose logs --tail=100 db  # postgres
sudo journalctl -u nginx -f        # host nginx (proxy/SSL)
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

Django LOGGING fayllar (host'da, `./logs/`):

```bash
tail -f logs/app.log    # barcha INFO+ darajasidagi xabarlar
tail -f logs/error.log  # faqat ERROR (django.request, jobs.runners)
```

> Ikkala daraja ham `RotatingFileHandler` orqali 10 MB × 5 faylgacha aylantiriladi.
> Kerak bo'lsa `.env`'da `LOG_LEVEL=DEBUG` qo'ying (lekin DB query/Telethon trafigi
> juda ko'p chiqadi).

### To'xtatish va qayta ishga tushirish

```bash
docker compose stop          # to'xtatish (data saqlanadi)
docker compose start         # qaytadan
docker compose restart web   # bitta servisni reboot
docker compose down          # to'liq olib tashlash (volumes saqlanadi)
docker compose down -v       # ⚠️ volumes ham o'chiriladi (DB yo'qoladi!)
```

### Django amallar (container ichida)

```bash
make shell             # Django shell
make migrate           # migratsiya
make makemigrations    # yangi migratsiya yaratish
make superuser         # admin user
make encrypt-secrets   # legacy plain session'larni shifrlash

# Yoki to'g'ridan-to'g'ri:
docker compose exec web /app/entrypoint.sh manage <subcommand>
```

### Worker holati

```bash
make worker-logs

# Stuck task'larni reset qilish (worker crash bo'lgandan keyin):
docker compose exec web /app/entrypoint.sh manage shell -c "
from jobs.models import Task
from django.utils import timezone
n = Task.objects.filter(status='running').update(
    status='failed', error='manual reset', finished_at=timezone.now()
)
print(f'{n} stuck task reset')
"
```

---

## .env to'liq sozlamalar

### Telegram

| Variable | Kerak | Tavsif |
|----------|-------|--------|
| `API_ID` | ✅ | https://my.telegram.org/auth dan |
| `API_HASH` | ✅ | shu yerdan |

Har akkaunt o'z `api_id`/`api_hash`'ini ham qo'shishi mumkin (Account model'da, UI orqali)
— bu yerdagi qiymat default fallback.

### Django

| Variable | Default | Tavsif |
|----------|---------|--------|
| `SECRET_KEY` | yo'q (crash bo'ladi) | Django sessiya/CSRF/passwords uchun |
| `DEBUG` | `False` | Production'da hech qachon `True` qilmang |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated |
| `SECURE_BEHIND_PROXY` | `False` | nginx orqasidasiz — `True` qiling |

### Database

| Variable | Default | Tavsif |
|----------|---------|--------|
| `DB_ENGINE` | `sqlite` | `sqlite` yoki `postgres` |
| `POSTGRES_DB` | `userbots` | DB nomi |
| `POSTGRES_USER` | `userbots` | DB user |
| `POSTGRES_PASSWORD` | yo'q | **Production'da shart** |
| `POSTGRES_HOST` | `db` | Docker'da servis nomi |
| `POSTGRES_PORT` | `5432` | |

### Encryption

| Variable | Default | Tavsif |
|----------|---------|--------|
| `DB_ENCRYPTION_KEY` | SECRET_KEY'dan derive | Fernet kalit. **Production'da alohida belgilang.** |

### Site / Proxy

| Variable | Default | Tavsif |
|----------|---------|--------|
| `SITE_DOMAIN` | `userbots.omadli.uz` | Public domen — host nginx + certbot ishlatadi |
| `WEB_BIND_PORT` | `8000` | Web container `127.0.0.1:<port>` ga bind bo'ladi (boshqa loyihalar bilan unique bo'lsin) |
| `WEB_WORKERS` | `2` | uvicorn worker soni |
| `LOG_LEVEL` | `INFO` | Root logger darajasi (`./logs/app.log`) |

### Backup → Telegram (ixtiyoriy, lekin tavsiya)

| Variable | Tavsif |
|----------|--------|
| `BACKUP_BOT_TOKEN` | @BotFather'dan olingan bot token. Bo'sh bo'lsa Telegram'ga yuborish o'chiriladi. |
| `BACKUP_CHAT_ID` | Bot a'zo bo'lgan kanal/chat ID (kanallar `-100…` bilan boshlanadi). `@userinfobot` orqali oling. |

---

## Backup va restore

### Avtomat backup (cron)

`crontab -e` ga qo'shing:

```cron
# Har kuni soat 03:00 da backup (Telegram'ga yuborish ham bajariladi
# agar BACKUP_BOT_TOKEN/BACKUP_CHAT_ID .env'da bo'lsa)
0 3 * * * cd /home/USER/apps/userbot-manager && make backup >> /var/log/userbots-backup.log 2>&1

# Lokal eski backup'larni o'chirish (30 kundan eski) — Telegram'da
# saqlanganlar tegmaydi.
0 4 * * * find /home/USER/apps/userbot-manager/backups/ -mindepth 1 -maxdepth 1 -mtime +30 -exec rm -rf {} +
```

### Qo'lda backup

```bash
make backup
# → backups/20260424_153000/db.sql.gz, media.tar.gz, .env.backup
# → BACKUP_BOT_TOKEN bo'lsa, fayllar Telegram chat'ga ham yuboriladi
```

### Mavjud backup'ni Telegram'ga qayta yuborish

```bash
make backup-telegram TS=20260424_153000
```

### Restore

```bash
make restore TS=20260424_153000
```

> Restore'da `.env`'ni qo'lda ko'chiring (yangi `DB_ENCRYPTION_KEY` bo'lmasin —
> backup vaqtidagi kalit kerak, aks holda sessiyalar o'qilmaydi).

### Telegram backup setup

1. [@BotFather](https://t.me/BotFather) bilan suhbatda `/newbot` — bot yarating, **token**'ni saqlang.
2. Faqat o'zingizga ko'rinadigan **private channel** yarating (yoki Saved Messages ishlatish mumkin).
3. Botni shu kanalga **admin** qilib qo'shing (post yuborish huquqi bilan).
4. Kanaldan biror xabarni `@userinfobot`'ga forward qiling — chat ID ni ko'rsatadi (kanallar uchun `-100…` formatida).
5. `.env`'ga yozing:
   ```env
   BACKUP_BOT_TOKEN=123456:ABC-DEF...
   BACKUP_CHAT_ID=-1001234567890
   ```
6. `make backup` — fayllar Telegram'da paydo bo'lishi kerak.

---

## Xavfsizlik

Loyiha quyidagilarni jonli ko'radi:

- **Multi-tenant** — har user faqat o'z akkauntlarini ko'radi (defense-in-depth ikki qavatli filter)
- **Field encryption** — `session_string` va `2FA password` Fernet bilan shifrlangan
- **Secrets in env** — `SECRET_KEY`, `DB_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` — hech qachon kodga yozilmagan
- **HTTPS-only** (production) — `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HSTS
- **Read-only secrets in admin** — sessiya/2FA admin panel'da edit qilinmaydi
- **Skript admin-only** — `run_script` faqat `is_superuser` uchun
- **Public signup yo'q** — yangi user faqat `/admin/` orqali

### Tavsiyalar

1. `.env` ni hech qachon git'ga qo'shmang (`.gitignore` allaqachon himoya qiladi)
2. `DB_ENCRYPTION_KEY` ni alohida xavfsiz joyda saqlang
3. Ssh-server uchun `PasswordAuthentication no`, faqat key auth
4. Server'da `ufw` yoki `firewalld` bilan faqat 22, 80, 443 portlarini oching
5. Docker socket'ga begona foydalanuvchi kirmasin (`/var/run/docker.sock`)
6. Telegram MTProto API ToS'iga rioya qiling — Telegram banni huquqi bor

---

## Muammolarni tuzatish

### `tgcrypto` o'rnatilmadi

Normal — Windows + Python 3.13'da binary wheel yo'q.
Telethon `pyaes`'ga avtomat fallback qiladi. Tezroq xohlasangiz Linux/WSL'da
`pip install tgcrypto`.

### "SECRET_KEY is not set"

`.env` da `SECRET_KEY` bo'sh yoki yo'q. Ishlab chiqaring:
```bash
make secrets
```

### certbot SSL olmayapti ("acme: error")

- DNS tarqalmagan: `dig +short userbots.omadli.uz` server IP qaytarishi kerak
- Port 80 yopiq: `sudo ufw allow 80/tcp; sudo ufw allow 443/tcp`
- Boshqa servis 80'ni ishlatmoqda (eski Caddy konteyneri qoldigan, va h.k.):
  `sudo ss -tlnp | grep ':80 '` — natija faqat `nginx` bo'lishi kerak
- Eski Caddy o'chirilmagan bo'lsa: `docker compose down caddy 2>/dev/null;
  docker rm -f $(docker ps -aq --filter name=caddy) 2>/dev/null`

### "Sessiya chiqarib yuborilgan"

Telegram akkauntdan chiqarib yuborgan. Sabab:
- Login qilingach darrov bulk amal (warming kerak)
- Bir IP'dan ko'p akkaunt (proxy ulang)
- `@spambot` ga login keyin darrov murojaat (alohida amal qiling, eskirgach)

Tuzatish: `/accounts/<id>/` → "Qayta kirish" tugmasi.

### Worker tasklarni ishlamayapti

```bash
docker compose ps                # worker ishlayaptimi
docker compose logs worker       # nima xato

# Reset stuck:
docker compose restart worker
docker compose exec web /app/entrypoint.sh manage shell -c "
from jobs.models import Task
print(Task.objects.filter(status='running').values_list('pk','kind','started_at'))
"
```

### "FloodWait too long"

Akkaunt operatsiyalari ko'p — pauza oshiring (Task params'da `delay_min_sec`,
`delay_max_sec`), parallel kamaytiring, kunlik byudjetni tushiring.

### DB migratsiyada konflikt

```bash
docker compose exec web /app/entrypoint.sh manage showmigrations
docker compose exec web /app/entrypoint.sh manage migrate --fake-initial
```

### Bog'lanmaydi: 502 Bad Gateway

Web container ishlamayapti yoki crash bo'lgan:
```bash
docker compose logs web --tail=200
docker compose restart web
```

### Disk to'lib qoldi

```bash
docker system prune -af          # ishlatilmaydigan image/container'lar
docker volume prune -f           # orphan volumes
journalctl --vacuum-time=7d      # tizim loglarini kichraytirish
```

---

## Loyiha tuzilmasi

```
.
├── accounts/               # Akkauntlar, proxy, teglar, kvota
├── channels/               # Channel modeli (akkauntning kanallari)
├── groups/                 # Group modeli
├── jobs/                   # Task framework + 9 ta runner
│   ├── runners.py          # Asosiy logika
│   ├── services.py         # Telethon helper'lar
│   └── management/commands/run_worker.py
├── config/                 # Django settings + URL
├── templates/              # Bootstrap 5 + Chart.js UI
├── Dockerfile              # Multi-stage Linux image
├── docker-compose.yml      # Production (web+worker+db) — host nginx alohida
├── docker-compose.dev.yml  # Lokal Docker
├── nginx/                  # Host nginx server bloklari (server'ga deploy qilinadi)
├── entrypoint.sh           # Container startup
├── Makefile                # Convenience targets
├── requirements.txt        # Python bog'liqliklar
└── .env.example, .env.docker.example
```

---

## Litsenziya / Mas'uliyat

Bu dastur Telegram MTProto API'sini userbot maqsadida ishlatadi.
Foydalanuvchi Telegram ToS va o'z mamlakati qonunlariga rioya qilishi shart.
Loyiha mualliflari foydalanuvchining harakatlari uchun mas'uliyatni
o'z zimmasiga olmaydi.

---

**Savol bo'lsa**: `/admin/` orqali admin foydalanuvchi yarating, keyin
loyihaning `Issue tracker`'iga yozing.
