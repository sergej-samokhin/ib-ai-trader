FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 1000 trader
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt .
USER trader
VOLUME ["/app/var", "/app/config"]
ENTRYPOINT ["ibtrader"]
CMD ["run"]
