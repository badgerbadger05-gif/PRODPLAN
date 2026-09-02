# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS builder
WORKDIR /app
ARG VITE_DEPLOYMENT_CONTOUR
ENV VITE_DEPLOYMENT_CONTOUR=$VITE_DEPLOYMENT_CONTOUR
ENV NODE_OPTIONS=--dns-result-order=ipv4first \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline

COPY . .
RUN npm run build

FROM nginx:1.27-alpine
ENV TZ=Europe/Moscow
RUN apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
RUN chmod -R a+rX /usr/share/nginx/html

EXPOSE 80
