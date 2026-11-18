import os

# فقط این خط را تغییر بده
TARGET_URL = "https://ourdream.ai/"

# پورت سرور (پلتفرم‌هایی مثل Render مقدار PORT را خودشان تنظیم می‌کنند)
PORT = int(os.environ.get("PORT", "10000"))

# به‌صورت پیش‌فرض آدرس‌های داخلی/لوکال (localhost، 10.x، 192.168.x ، 169.254.x)
# مسدودند تا پروکسی به شبکهٔ داخلی سرور راه نداشته باشد (SSRF).
# فقط برای آزمایش محلی: ALLOW_PRIVATE_HOSTS=1
ALLOW_PRIVATE_HOSTS = os.environ.get("ALLOW_PRIVATE_HOSTS", "").lower() in (
    "1", "true", "yes",
)
