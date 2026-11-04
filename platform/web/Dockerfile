# The desk's web app as a static image. The browser calls the API directly, so its public URL and Clerk's
# (public) publishable key are baked into the bundle: infra/up.sh passes both.
FROM node:22.11.0-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_API_URL=http://localhost:8100
ARG VITE_CLERK_PUBLISHABLE_KEY=
ARG VITE_CLERK_JWT_TEMPLATE=loandesk
ENV VITE_API_URL=$VITE_API_URL VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY \
    VITE_CLERK_JWT_TEMPLATE=$VITE_CLERK_JWT_TEMPLATE
RUN npm run build

FROM nginx:1.27.3-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
