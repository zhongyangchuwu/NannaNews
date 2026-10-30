# Build frontend code without a public package; runtime data is staged explicitly.
FROM node:22.14.0-bookworm-slim AS reader-build
WORKDIR /reader
COPY reader/package.json reader/package-lock.json ./
RUN npm ci
COPY reader/index.html reader/tsconfig.json reader/vite.config.ts ./
COPY reader/src ./src
RUN npm exec vite build

FROM reader-build AS reader-runtime
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

FROM ghcr.io/typst/typst:0.15.1 AS typst
FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NANNA_DATA_DIR=/data NANNA_HOST=0.0.0.0 NANNA_PORT=8080 \
    NANNA_TYPST_BINARY=/usr/local/bin/typst \
    NANNA_TYPST_PACKAGE_CACHE=/opt/typst/packages

# These exact Debian font releases supply both CJK faces and link monospace.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl fontconfig libcairo2 libatomic1 libstdc++6 poppler-utils util-linux \
        fonts-noto-cjk=1:20220127+repack1-1 fonts-dejavu-core=2.37-6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=typst /bin/typst /usr/local/bin/typst
COPY --from=reader-build /usr/local/bin/node /usr/local/bin/node
COPY --from=reader-build /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Pin and pre-cache the imports used by the reviewed templates; rendering is offline.
RUN set -eu; \
    for package in tiaoma:0.3.0 wrap-it:0.1.1; do \
        name="${package%:*}"; version="${package#*:}"; \
        directory="/opt/typst/packages/preview/${name}/${version}"; \
        mkdir -p "$directory"; \
        curl --fail --show-error --location --retry 2 \
            "https://packages.typst.org/preview/${name}-${version}.tar.gz" \
            --output /tmp/package.tar.gz; \
        tar -xzf /tmp/package.tar.gz -C "$directory"; \
        test -f "$directory/typst.toml"; \
    done; \
    rm /tmp/package.tar.gz; \
    chmod -R a+rX /opt/typst

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/nannanews ./src/nannanews
COPY reader/package.json reader/package-lock.json ./reader/
COPY reader/scripts ./reader/scripts
COPY reader/src/tokenize.mjs reader/src/review-validation.mjs ./reader/src/
COPY --from=reader-runtime /reader/node_modules ./reader/node_modules
COPY --from=reader-build /reader/dist /opt/nanna-reader-site
COPY deploy/stage-reader.sh ./deploy/stage-reader.sh
RUN --mount=type=secret,id=pip_index_url,env=PIP_INDEX_URL \
    pip install --no-cache-dir pip==25.1.1 \
    && pip install --no-cache-dir .
RUN groupadd --gid 10001 nanna \
    && useradd --uid 10001 --gid nanna --create-home nanna \
    && mkdir -p /data \
    && chown nanna:nanna /data \
    && chmod 700 /data
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3).read(1)" || exit 1
ENTRYPOINT ["nannanews"]
CMD ["serve"]
