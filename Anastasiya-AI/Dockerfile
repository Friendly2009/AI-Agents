FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt requirements-docker.lock ./
RUN python -m pip install --no-cache-dir -r requirements.txt -c requirements-docker.lock && python -m pip check
COPY src/ src/
COPY scripts/launch.py scripts/launch.py
COPY resources/ resources/
CMD ["python", "scripts/launch.py", "main", "listen"]
