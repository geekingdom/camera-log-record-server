# 本机容器构建验收阻塞

源码基线`d78874c`。本轮从采集核心合成验证转向部署边界，没有修改部署配置或业务源码。

## 已核验

- Docker Desktop原先未运行，启动后服务端版本29.4.0，无运行中的容器。
- `scripts/verify_build_context.py`通过：5个必要文件及19个排除场景。验证器使用隔离构建上下文，没有将当前真实日志和凭据复制入镜像。
- 正式`deploy/worker.Dockerfile`尝试使用独立标签`camera-log-worker:verify-d78874c`构建；仅通过build args覆盖公网PyPI，未修改公司默认源。
- 构建在获取`docker.io/library/python:3.12-slim`元数据时终止，错误为访问`registry-1.docker.io`的HEAD请求`context deadline exceeded`。独立宿主机curl连接443也在5秒超时。
- 未进入COPY工程源码或pip安装步骤，未产生该临时标签的镜像，也没有创建采集任务、测试容器或日志卷。

## 边界与下一步

此失败是基础镜像仓库访问阻塞，不能判为工程依赖安装或运行失败，也不能宣称当前Docker镜像构建通过。已有旧镜像不代表当前提交产物，不改标签冒充验收。

`verify_component_deployment.py`明确仅在Linux执行，依赖原生host网络拓扑；Docker Desktop不作为原生Linux部署的等价证明。恢复可信Docker Hub访问或提供批准的基础镜像缓存后重跑构建；独立组件及原生部署仍需目标Linux环境。GitHub CI账号计费限制另见总表，两者是不同外部条件。

保留原有Docker镜像及数据卷，没有执行全局prune。Docker引擎保持启动，无新增运行容器；无本轮采集日志待清理。
