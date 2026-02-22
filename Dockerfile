FROM node:19 as build
WORKDIR /usr/src/app    
COPY package.json package-lock.json ./
RUN npm install
COPY .eslintrc.json webpack.config.js tsconfig.json .prettierignore .editorconfig ./
COPY src ./src
COPY relay.config.js ./
RUN npm run build

FROM python:3.11-bullseye as runner
WORKDIR /opt/servermonitor
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY indexer_utils ./indexer_utils
COPY .env *.py ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY --from=build /usr/src/app/webpack-stats.json .
COPY gunicorn_start .
CMD alembic upgrade head && ./gunicorn_start

FROM nginx as host
COPY --from=build /usr/src/app/frontend/static /data
COPY nginx.conf /etc/nginx/nginx.conf
COPY authelia.conf /etc/nginx/authelia.conf
COPY ssh.conf /etc/nginx/ssh.conf
COPY auth.conf /etc/nginx/auth.conf
COPY auth.users /etc/nginx/auth.users
COPY ssl /etc/ssl
