FROM node:22-alpine AS build
WORKDIR /src
COPY frontend/package*.json ./
# 公司构建默认使用内网制品库；外部 CI 通过 build arg 切换到公网 npm 源。
ARG NPM_REGISTRY=http://af.hikvision.com.cn/artifactory/api/npm/npm-hiktest/
ARG NPM_STRICT_SSL=false
RUN npm config set registry "$NPM_REGISTRY" \
    && npm config set strict-ssl "$NPM_STRICT_SSL" \
    && npm ci
COPY frontend ./
RUN npm run build

FROM nginx:1.27-alpine
ENV BACKEND_UPSTREAM=http://api:18080
# Nginx 官方入口仅展开环境变量；单独前端可将 API 代理指向其它服务器。
COPY deploy/nginx/frontend.conf /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
