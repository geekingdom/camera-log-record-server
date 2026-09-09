FROM node:22-alpine AS build
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM nginx:1.27-alpine
ENV BACKEND_UPSTREAM=http://api:8000
# Nginx 官方入口仅展开环境变量；单独前端可将 API 代理指向其它服务器。
COPY deploy/nginx/frontend.conf /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
