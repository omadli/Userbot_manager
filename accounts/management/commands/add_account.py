from django.core.management.base import BaseCommand
from accounts.models import Account
from accounts.services import verify_login, send_code
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

class Command(BaseCommand):
    help = 'Add a new telegram account account via terminal'

    def handle(self, *args, **options):
        phone_number = input("Telefon raqamini kiriting (masalan. +998901234567): ")
        
        if Account.objects.filter(phone_number=phone_number).exists():
            self.stdout.write(self.style.WARNING("Bu raqam bazada allaqachon mavjud."))
            return
            
        self.stdout.write(self.style.SUCCESS(f"{phone_number} ga kod yuborilmoqda..."))
        
        send_result = async_to_sync(send_code)(phone_number)
        if not send_result["success"]:
            self.stdout.write(self.style.ERROR(f"Xatolik: {send_result.get('error')}"))
            return
            
        code = input("Telegramdan kelgan kodni kiriting: ")
        
        verify_result = async_to_sync(verify_login)(
            phone_number=phone_number, 
            phone_code_hash=send_result["phone_code_hash"], 
            code=code, 
            temp_session=send_result["session_string"]
        )
        
        if verify_result.get("needs_password"):
            self.stdout.write(self.style.WARNING("Ushbu hisobda ikki bosqichli parol o'rnatilgan (2FA)."))
            password = input("Parolni kiriting: ")
            email = input("Email manzilini kiriting (Ixtiyoriy, Enter = o'tkazib yuborish): ")
            
            verify_result = async_to_sync(verify_login)(
                phone_number=phone_number, 
                phone_code_hash=send_result["phone_code_hash"], 
                code=code, 
                temp_session=verify_result.get("session_string", send_result["session_string"]),
                password=password,
                email=email if email.strip() else None
            )

        if verify_result.get("success"):
            User = get_user_model()
            owner = User.objects.first()
            
            account, created = Account.objects.update_or_create(
                phone_number=phone_number,
                defaults={
                    "session_string": verify_result["session_string"],
                    "is_spam": verify_result.get("is_spam", False),
                    "user_id": verify_result.get("user_id"),
                    "first_name": verify_result.get("first_name", ""),
                    "last_name": verify_result.get("last_name", ""),
                    "username": verify_result.get("username", ""),
                    "owner": owner
                }
            )
            self.stdout.write(self.style.SUCCESS(f"Muvaffaqiyatli ulashildi: {phone_number}!"))
            if account.is_spam:
                self.stdout.write(self.style.WARNING("DIQQAT: Bu hisob spam holatida ekanligi aniqlandi!"))
        else:
            self.stdout.write(self.style.ERROR(f"Xatolik tasdiqlashda yuz berdi: {verify_result.get('error')}"))
