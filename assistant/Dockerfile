FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --ignore-scripts --omit=dev --no-audit --no-fund --fetch-retries=5
COPY src ./src
USER node
EXPOSE 3000
CMD ["node", "src/server.mjs"]
