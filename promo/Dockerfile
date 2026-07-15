FROM node:20-alpine AS base
RUN npm install -g pnpm@9.12.0

FROM base AS deps
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY promo/package.json ./promo/
RUN pnpm install --frozen-lockfile

FROM base AS builder
WORKDIR /app
ARG NEXT_PUBLIC_BASE_PATH=/promo
ENV NEXT_PUBLIC_BASE_PATH=${NEXT_PUBLIC_BASE_PATH}
COPY --from=deps /app ./
COPY promo ./promo
RUN pnpm --filter openreel-promo build

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV HOSTNAME=0.0.0.0
ENV PORT=3001
COPY --from=builder /app/promo/.next/standalone ./
COPY --from=builder /app/promo/.next/static ./promo/.next/static
EXPOSE 3001
CMD ["node", "promo/server.js"]
