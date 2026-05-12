# Stage 1: extract app source from the existing image
FROM weaveworksdemos/front-end:0.3.12 AS source

# Stage 2: rebuild on Node.js 18 LTS
FROM node:18-alpine

WORKDIR /usr/src/app

# Copy application source files from the original image
COPY --from=source /usr/src/app/api ./api
COPY --from=source /usr/src/app/helpers ./helpers
COPY --from=source /usr/src/app/public ./public
COPY --from=source /usr/src/app/config.js .
COPY --from=source /usr/src/app/server.js .
COPY --from=source /usr/src/app/package.json .

# redis is a peer dep of connect-redis@3.x but not a direct dep in the original package.json.
# --ignore-scripts skips optional native builds (hiredis); redis falls back to pure JS.
RUN npm install --omit=dev --ignore-scripts && \
    npm install redis@2 --omit=dev --ignore-scripts

# Match the non-root UID used by the original image
RUN addgroup -g 10001 -S appuser && adduser -u 10001 -S -G appuser appuser
USER 10001

EXPOSE 8079
CMD ["node", "server.js"]
