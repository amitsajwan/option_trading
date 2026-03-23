FROM node:20-alpine AS build

WORKDIR /app

COPY strategy_eval_ui/package.json /app/package.json
COPY strategy_eval_ui/tsconfig.json /app/tsconfig.json
COPY strategy_eval_ui/tsconfig.app.json /app/tsconfig.app.json
COPY strategy_eval_ui/tsconfig.node.json /app/tsconfig.node.json
COPY strategy_eval_ui/vite.config.ts /app/vite.config.ts
COPY strategy_eval_ui/index.html /app/index.html
COPY strategy_eval_ui/src /app/src

RUN npm install
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8011

RUN printf "server {\n\
    listen 8011;\n\
    server_name _;\n\
    root /usr/share/nginx/html;\n\
    index index.html;\n\
    location / {\n\
        try_files \$uri \$uri/ /index.html;\n\
    }\n\
}\n" > /etc/nginx/conf.d/default.conf
