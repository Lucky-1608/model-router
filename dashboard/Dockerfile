FROM node:24-alpine AS base

WORKDIR /app

COPY package.json ./
RUN npm install --no-audit --no-fund

COPY . .

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["npm", "run", "dev"]

