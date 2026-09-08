# ============================================================
# Gojgui Pro — صورة Docker (Python 3.11 + Node.js 20 LTS)
# مثالية للنشر على Railway: يتم اكتشاف الملف تلقائياً
# ============================================================

FROM python:3.11-slim

# ---- تثبيت Node.js 20 LTS (لدعم استضافة تطبيقات Node.js) ----
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- مكتبات Python (طبقة مستقلة = بناء أسرع عند تحديث الكود فقط) ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- ملفات المشروع ----
COPY . .

# دليل البيانات: على Railway صل Volume بمسار /data حتى لا تُفقد
# الحسابات والخوادم عند إعادة النشر (متوافق أيضاً بدون Volume)
RUN mkdir -p /data
ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1

# Railway يوفر متغير PORT تلقائياً — app.py يقرأه ويستمع عليه
EXPOSE 8080

CMD ["python", "app.py"]
