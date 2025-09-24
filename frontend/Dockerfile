FROM node:20-alpine AS build-stage
WORKDIR /app

# Копируем конфигурационные файлы ПЕРВЫМИ
COPY package*.json ./
COPY quasar.config.js ./
COPY tsconfig.json ./
COPY package-lock.json ./

# Устанавливаем зависимости
RUN npm ci

# Копируем исходный код
COPY src/ ./src/
COPY public/ ./public/
COPY index.html ./

# Собираем приложение
RUN npm run build

FROM nginx:alpine AS production-stage

# Копируем собранное приложение
COPY --from=build-stage /app/dist/spa /usr/share/nginx/html

# Копируем конфигурацию nginx
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]