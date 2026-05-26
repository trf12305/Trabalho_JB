FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cria diretório de dados se não existir na imagem
RUN mkdir -p data

EXPOSE 5000

ENV FLASK_ENV=production

CMD ["python", "run.py"]
