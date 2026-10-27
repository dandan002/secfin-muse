FROM node:22.11.0-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# The browser talks to mock-ib directly, so this must be a URL the browser can reach.
ARG VITE_IB_URL=http://localhost:8010
ENV VITE_IB_URL=$VITE_IB_URL
RUN npm run build

FROM nginx:1.27.3-alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
