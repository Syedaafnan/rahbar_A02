# syntax=docker/dockerfile:1.7
# RAHBAR ui: static chat page served by unprivileged nginx on 8080.
# Build context: ui/        docker build -t rahbar-ui:<sha> ui
#
# The pod runs with readOnlyRootFilesystem: true; nginx.conf writes only under /tmp,
# so the Deployment mounts an emptyDir there.
# 1.30.6 on Alpine 3.24: the old 1.27.5 image (Alpine 3.21.3) had 65 HIGH/CRITICAL
# findings with fixes available in its system libraries (Trivy, CI scan job).
FROM nginxinc/nginx-unprivileged:1.30.6-alpine3.24
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
# nginx-unprivileged already runs as uid 101; state it so the checker and the
# reader do not have to know that.
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
