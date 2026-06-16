# Public DCS demo container.
# Runs the survey app in --demo mode: empty database, PII features disabled,
# simulated device. Reports render via the reportlab fallback (no browser
# engine needed), keeping the image small.
FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY requirements-demo.txt .
RUN pip install --no-cache-dir -r requirements-demo.txt

COPY . .

ENV DCS_DEMO=1
ENV PYTHONUNBUFFERED=1

# The platform sets $PORT; app.py binds 0.0.0.0:$PORT when PORT is present.
CMD ["python", "app.py", "--demo", "--no-browser"]
