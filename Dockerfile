# =============================================================================
# Builder stage - UV for fast dependency installation
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Best Practice: Copy uv.lock alongside pyproject.toml for reproducible builds
COPY pyproject.toml uv.lock* ./

# Create virtual environment and install only python dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv sync --no-dev --no-install-project

# =============================================================================
# Final stage - Runtime & Browsers
# =============================================================================
FROM python:3.13-slim-bookworm

WORKDIR /app

# 1. Copy virtual environment from builder BEFORE installing browsers
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

# 2. Install minimal basics needed for browser download tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# 3. Let Playwright do the heavy lifting of installing system dependencies (--with-deps)
#    and the actual Chromium binary straight into the final image
RUN playwright install --with-deps chromium

# 4. Install Google Chrome for SeleniumBase
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# 5. Install SeleniumBase chromedriver (downloads directly to the final image)
RUN seleniumbase get chromedriver

# 5. Copy the source code last (so code changes don't invalidate the browser cache!)
COPY src/ /app/src/

# Create data directory
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]