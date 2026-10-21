
# Build the React frontend
FROM node:20-alpine AS frontend-builder

WORKDIR /app

COPY chat-frontend/package*.json ./
RUN npm ci

COPY chat-frontend/ ./

ARG VITE_API_URL=/api
ARG VITE_WS_URL=

ENV VITE_API_URL=$VITE_API_URL
ENV VITE_WS_URL=$VITE_WS_URL

RUN npm run build

# Serve the frontend and proxy requests through Nginx
FROM nginx:1.28-alpine

COPY backend/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-builder /app/dist/ /usr/share/nginx/html/

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
