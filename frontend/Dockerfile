FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

ARG VITE_API_BASE=http://localhost:5001
ENV VITE_API_BASE=$VITE_API_BASE

EXPOSE 5000

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
