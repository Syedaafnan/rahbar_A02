# syntax=docker/dockerfile:1.7
# RAHBAR ui: static chat page served by nginx on 8080, as a non-root user.
# Build context: ui/        docker build -t rahbar-ui:<sha> ui
#
# The pod runs with readOnlyRootFilesystem: true; nginx.conf writes only under /tmp,
# so the Deployment mounts an emptyDir there.
# Alpine's own nginx package instead of nginxinc/nginx-unprivileged: the newest
# nginx-unprivileged 1.30 image still ships nginx 1.30.6, which Trivy flags with 4
# CRITICAL and 2 HIGH CVEs (CVE-2026-113425 and others) fixed in Alpine's 1.30.7-r0.
FROM alpine:3.24
RUN apk upgrade --no-cache \
 && apk add --no-cache nginx \
 && mkdir -p /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
# The pod sets runAsUser 101 / runAsGroup 101; state it here too.
USER 101
EXPOSE 8080
CMD ["nginx", "-e", "/dev/stderr", "-g", "daemon off;"]
