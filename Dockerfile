FROM python:3.13-slim
WORKDIR /app
COPY ledgerlab ./ledgerlab
COPY web ./web
ENV LEDGER_HOST=0.0.0.0 LEDGER_PORT=8080 LEDGER_DB=/data/ledger.sqlite3
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "ledgerlab"]
