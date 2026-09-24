# Hardware is detected by sandbox.build on the selected Docker daemon.
# Docker builds themselves do not expose the host GPU; use the unified launcher.
ARG BASE_IMAGE=python:3.12-slim-bookworm
# Keep server versions explicit; update them together with the smoke checks.
FROM node:22-bookworm-slim AS node_lsp
COPY dependencies/node/package*.json /opt/lsp/
RUN npm ci --prefix /opt/lsp --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

FROM golang:1.25-bookworm AS go_lsp
ARG GOPLS_VERSION=v0.20.0
RUN CGO_ENABLED=0 GOBIN=/out go install golang.org/x/tools/gopls@${GOPLS_VERSION}

FROM ${BASE_IMAGE}
ARG SANDBOX_PROFILE=standard
LABEL org.repo-agent.profile=${SANDBOX_PROFILE}
RUN apt-get update && apt-get install -y --no-install-recommends \
    git clangd clang build-essential ninja-build ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node_lsp /usr/local/bin/ /usr/local/bin/
COPY --from=node_lsp /opt/lsp/ /opt/lsp/
COPY --from=go_lsp /usr/local/go/ /usr/local/go/
COPY --from=go_lsp /out/gopls /usr/local/bin/gopls
ENV PATH="/opt/lsp/node_modules/.bin:/usr/local/go/bin:/usr/local/cuda/bin:${PATH}" \
    CUDA_HOME=/usr/local/cuda \
    PYTHONDONTWRITEBYTECODE=1 \
    TORCH_EXTENSIONS_DIR=/tmp/torch-extensions \
    TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor \
    TRITON_CACHE_DIR=/tmp/triton CUDA_CACHE_PATH=/tmp/cuda-cache \
    MAX_JOBS=2 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
    XDG_CACHE_HOME=/tmp/lsp-cache \
    GOPATH=/tmp/go GOCACHE=/tmp/go-build GOMODCACHE=/tmp/go-mod \
    GOTOOLCHAIN=local GOTELEMETRY=off GOPROXY=off GOSUMDB=off GOMAXPROCS=2
WORKDIR /opt/agent
# .dockerignore is generated from build_manifest.py's explicit allowlist.
COPY . ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-build.lock -r requirements-lsp.lock \
    && python -m pip install --no-cache-dir --no-build-isolation --no-deps . \
    && git config --system --add safe.directory /workspace \
    && case "$SANDBOX_PROFILE" in \
         cuda) python -m pip install --no-cache-dir pytest ninja triton==3.3.1 ;; \
         standard) ;; \
         *) echo "Invalid SANDBOX_PROFILE" >&2; exit 1 ;; \
       esac
# Fail builds early when any bundled server cannot list a basic source symbol.
RUN if [ "$SANDBOX_PROFILE" = cuda ]; then \
      python -c "import torch, triton; assert torch.__version__.split('+')[0] == '2.7.1'; assert torch.version.cuda == '12.8'; assert triton.__version__ == '3.3.1'" \
      && nvcc --version && python -I -m sandbox.lsp_smoke --cuda; \
    else python -I -m sandbox.lsp_smoke; fi
ENTRYPOINT ["python", "-I", "-m", "sandbox.worker"]
