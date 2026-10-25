# syntax=docker/dockerfile:1
FROM debian:bookworm-slim AS browser
ARG CHROME_VERSION=156.0.8078.47
ARG TARGETOS
ARG TARGETARCH
RUN case "${TARGETOS}/${TARGETARCH}" in \
    linux/amd64|linux/arm64) ;; \
    *) echo "Unsupported platform: ${TARGETOS}/${TARGETARCH}" >&2; exit 1 ;; \
    esac
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip fonts-liberation \
    fonts-noto-core fonts-noto-cjk fonts-noto-extra fonts-noto-color-emoji \
    fonts-noto-mono fonts-roboto-unhinted \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
    libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxkbcommon0 libxrandr2 xdg-utils xvfb xauth tini \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
    amd64) chrome_platform=linux64 ;; \
    arm64) chrome_platform=linux-arm64 ;; \
    esac \
    && curl -fsSL --retry 3 "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/${chrome_platform}/chrome-${chrome_platform}.zip" -o /tmp/chrome.zip \
    && unzip -q /tmp/chrome.zip -d /opt \
    && rm /tmp/chrome.zip \
    && mv "/opt/chrome-${chrome_platform}" /opt/chrome \
    && /opt/chrome/chrome --version
RUN groupadd -g 10001 webcappi && useradd -u 10001 -g 10001 -m webcappi \
    && mkdir -p /work && chown 10001:10001 /work
ENV HOME=/work/home TMPDIR=/work XDG_CONFIG_HOME=/work/config XDG_CACHE_HOME=/work/cache
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--"]

FROM browser AS test-browser
USER root
RUN apt-get update && apt-get install -y --no-install-recommends xdotool && rm -rf /var/lib/apt/lists/*
USER 10001:10001

FROM --platform=$BUILDPLATFORM golang:1.26.10-bookworm AS build
ARG TARGETOS
ARG TARGETARCH
ARG WEBCAPPI_VERSION=dev
ARG WEBCAPPI_REVISION=unknown
ARG WEBCAPPI_MODIFIED=unknown
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" go build -trimpath -buildvcs=false \
    -ldflags "-X github.com/dayflower/webcappi/internal/buildinfo.Version=${WEBCAPPI_VERSION} -X github.com/dayflower/webcappi/internal/buildinfo.Revision=${WEBCAPPI_REVISION} -X github.com/dayflower/webcappi/internal/buildinfo.Modified=${WEBCAPPI_MODIFIED}" \
    -o /out/webcappi ./cmd/webcappi

FROM scratch AS binary
COPY --from=build /out/webcappi /webcappi

FROM browser AS runtime
ARG WEBCAPPI_VERSION=dev
ARG WEBCAPPI_REVISION=unknown
ARG WEBCAPPI_MODIFIED=unknown
LABEL org.opencontainers.image.source="https://github.com/dayflower/webcappi" \
      org.opencontainers.image.version="${WEBCAPPI_VERSION}" \
      org.opencontainers.image.revision="${WEBCAPPI_REVISION}" \
      io.github.dayflower.webcappi.modified="${WEBCAPPI_MODIFIED}"
COPY --from=build /out/webcappi /usr/local/bin/webcappi
EXPOSE 8080
CMD ["webcappi", "serve", "-config", "/etc/webcappi/config.json"]
