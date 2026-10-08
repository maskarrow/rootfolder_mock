# syntax=docker/dockerfile:1

# The web image: the Next.js server in standalone mode.
#
# No environment-specific value is baked in. `API_INTERNAL_URL` is read when the
# server starts; only `APP_VERSION`, the build's own identity, goes into the build
# (the build ID and the footer).

ARG NODE_IMAGE=node:22.23.1-slim

FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
# Needs internet: `next/font/google` downloads the font here, not at run time.
RUN npm run build

FROM ${NODE_IMAGE}
WORKDIR /app
# HOSTNAME: Docker sets it to the container id, and the Next server listens on
# whatever it finds there.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
# `standalone` leaves out the static assets; without them the page has no CSS or
# JavaScript. (This app has no `public/` folder; Delegate's would be copied too.)
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
# The image has no curl. `/login` renders without a session.
HEALTHCHECK --interval=10s --timeout=10s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "server.js"]
