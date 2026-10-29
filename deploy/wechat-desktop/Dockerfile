# Official Ubuntu 24.04 amd64 image observed on 2026-10-19.
FROM ubuntu:24.04@sha256:a2d43bffc46670d7108bb3c0468c67c5557c4c92e83e1e34cbff1322ad1b94dc

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The vendor deb declares only fonts; its ELF dependencies also require these
# distro libraries. Keep distro security updates; pin the vendor payload below.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        at-spi2-core ca-certificates curl dbus dbus-x11 desktop-file-utils \
        fonts-noto-cjk fonts-noto-color-emoji \
        libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatomic1 libatspi2.0-0t64 \
        libcairo2 libcups2t64 libdbus-1-3 libegl1 libexpat1 libfontconfig1 \
        libgbm1 libgl1 libgl1-mesa-dri libglib2.0-0t64 libglib2.0-bin \
        libice6 libjack-jackd2-0 libnspr4 libnss3 libpango-1.0-0 libpulse0 \
        libsm6 libstdc++6 libudev1 libx11-6 libx11-xcb1 libxcb1 \
        libxcb-keysyms1 libxcb-shm0 libxcb-xfixes0 libxcomposite1 libxdamage1 \
        libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
        novnc openbox openssl scrot shared-mime-info websockify x11-utils \
        x11vnc xauth xdotool xvfb zlib1g \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 240 \
        --output /tmp/wechat.deb \
        https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb \
    && echo 'a0db8caeee8d1e6cfe0aa5ce58710270f8d74a90ff4920f53a3fc22aa0956fe9  /tmp/wechat.deb' | sha256sum --check --strict \
    && test "$(dpkg-deb --field /tmp/wechat.deb Package)" = wechat \
    && test "$(dpkg-deb --field /tmp/wechat.deb Version)" = 4.1.13.26 \
    && test "$(dpkg-deb --field /tmp/wechat.deb Architecture)" = amd64 \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /tmp/wechat.deb \
    && rm /tmp/wechat.deb \
    && rm -rf /var/lib/apt/lists/* \
    && dbus-uuidgen > /etc/machine-id \
    && groupadd --gid 10001 wechat \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/wechat --shell /usr/sbin/nologin wechat \
    && chmod 0700 /home/wechat \
    && install -d -o 10001 -g 10001 -m 0700 \
        /home/wechat/.config /home/wechat/.cache /home/wechat/.local/share \
        /home/wechat/.local/state /home/wechat/.vnc

# The official client loads Wayland shims even in the isolated X11 desktop.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libwayland-client0 libwayland-cursor0 libwayland-egl1 xclip \
        tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0555 start-desktop.sh /usr/local/bin/start-desktop
COPY --chmod=0555 desktop-health.sh /usr/local/bin/desktop-health

ENV HOME=/home/wechat \
    LANG=C.UTF-8 \
    DISPLAY=:99 \
    XAUTHORITY=/tmp/wechat-desktop/Xauthority \
    XDG_RUNTIME_DIR=/tmp/wechat-desktop \
    XDG_CONFIG_HOME=/home/wechat/.config \
    XDG_CACHE_HOME=/home/wechat/.cache \
    XDG_DATA_HOME=/home/wechat/.local/share \
    XDG_STATE_HOME=/home/wechat/.local/state \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/wechat-desktop/bus \
    LIBGL_ALWAYS_SOFTWARE=1 \
    QT_ACCESSIBILITY=1 \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    GTK_A11Y=always

LABEL org.opencontainers.image.title="NannaNews dedicated official Weixin desktop" \
    org.opencontainers.image.version="4.1.13.26" \
    org.opencontainers.image.source="https://linux.weixin.qq.com/en"

USER 10001:10001
WORKDIR /home/wechat
EXPOSE 6080
ENTRYPOINT ["/usr/local/bin/start-desktop"]
