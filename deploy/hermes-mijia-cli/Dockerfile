FROM python:3.12-slim-bookworm

ARG MIJIA_VERSION=5.0.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server openssh-client bash ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "mijiaAPI==${MIJIA_VERSION}" \
    && useradd --create-home --uid 1000 --shell /bin/bash agent \
    && passwd -d agent \
    && mkdir -p /run/sshd /etc/ssh/authorized_keys
COPY sshd_config /etc/ssh/sshd_config
COPY warm_catalog.py /opt/mijia/warm_catalog.py
COPY mijia_house_policy.py /usr/local/lib/python3.12/site-packages/mijia_house_policy.py
COPY mijia_cli.py /usr/local/bin/mijiaAPI
RUN chmod 755 /usr/local/bin/mijiaAPI
COPY entrypoint.sh /opt/mijia/entrypoint.sh
EXPOSE 2222
CMD ["/bin/sh", "/opt/mijia/entrypoint.sh"]
